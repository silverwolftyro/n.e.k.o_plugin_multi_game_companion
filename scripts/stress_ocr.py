# ruff: noqa: E402
"""OCR 链路独立进程压测（L2，脱离宿主）。

拍板 2.0.70：模拟生产场景跑 N 分钟（默认 1 小时），每 10 秒打一次指标：
  - 完成次数 / 失败次数 / 超时次数
  - RSS 内存（psutil）
  - 活动线程数
  - Windows 进程 handle 数（ctypes GetProcessHandleCount；非 Windows 跳过）

跑法：
  python scripts/stress_ocr.py --duration 600 --rate 10
  python scripts/stress_ocr.py --duration 60 --rate 5 --report-json out.json

环境：
  - 不依赖宿主 plugin host
  - 不写 ocr_engine.py，只调
  - WinRT OCR 可用就用，没有就 mock（确认链路能跑）

用途：找内存/线程/handle 泄漏、定位 watch-dog 死角。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 允许从任意 cwd 跑
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugin" / "plugins" / "multi_game_companion"
sys.path.insert(0, str(REPO_ROOT))

from plugin.plugins.multi_game_companion.ocr_engine import OcrEngine  # noqa: E402
from plugin.plugins.multi_game_companion.screen_capture import (  # noqa: E402
    CaptureResult,
    capture_active_frame,
)

# =============================================================================
# Metrics
# =============================================================================


@dataclass
class Metrics:
    """聚合指标——单线程用。"""

    started_at: float = field(default_factory=time.monotonic)
    completed: int = 0
    failed: int = 0
    timeouts: int = 0
    total_dt_sum: float = 0.0  # 用于算平均耗时
    last_capture_ok: bool | None = None
    last_text_len: int = 0
    samples: int = 0

    def add_success(self, dt: float, capture_ok: bool, text_len: int) -> None:
        self.completed += 1
        self.total_dt_sum += dt
        self.last_capture_ok = capture_ok
        self.last_text_len = text_len
        self.samples += 1

    def add_failure(self, dt: float, reason: str) -> None:
        self.failed += 1
        self.total_dt_sum += dt
        self.samples += 1

    def add_timeout(self, dt: float) -> None:
        self.timeouts += 1
        self.total_dt_sum += dt
        self.samples += 1

    def summary(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self.started_at
        avg_dt = self.total_dt_sum / max(self.samples, 1)
        return {
            "elapsed_s": round(elapsed, 2),
            "completed": self.completed,
            "failed": self.failed,
            "timeouts": self.timeouts,
            "rate_per_s": round(self.completed / max(elapsed, 0.001), 3),
            "avg_iter_dt_s": round(avg_dt, 4),
        }


# =============================================================================
# System metrics
# =============================================================================


def _rss_mb() -> float:
    """RSS 内存（MB）。优先 psutil，fallback resource。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except ImportError:
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except ImportError:
            return -1.0


def _handle_count() -> int:
    """Windows 进程 handle 数。非 Windows 返回 -1。"""
    if sys.platform != "win32":
        return -1
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        GetProcessHandleCount = kernel32.GetProcessHandleCount
        GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        handle = wintypes.HANDLE(kernel32.GetCurrentProcess())
        if GetProcessHandleCount(handle, ctypes.byref(count)):
            return count.value
        return -1
    except Exception:
        return -1


def _active_threads() -> int:
    return threading.active_count()


def _snapshot() -> dict[str, Any]:
    return {
        "rss_mb": round(_rss_mb(), 2),
        "threads": _active_threads(),
        "handles": _handle_count(),
    }


# =============================================================================
# Stress loop
# =============================================================================


class StressRunner:
    """主循环：每 --rate 秒触发一次 OCR，超时则记 timeout。"""

    def __init__(
        self,
        *,
        rate: float,
        duration: float,
        per_iter_timeout: float = 30.0,
        report_every: float = 10.0,
        on_report: Any = None,
    ) -> None:
        self.rate = max(0.01, rate)  # 次/秒
        self.interval = 1.0 / rate
        self.duration = duration
        self.per_iter_timeout = per_iter_timeout
        self.report_every = report_every
        self.on_report = on_report

        self.metrics = Metrics()
        self._stop_event = threading.Event()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stress-ocr")
        self._last_report_at = time.monotonic()
        self._baseline = _snapshot()

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> dict[str, Any]:
        print(f"[stress_ocr] start: rate={self.rate}/s duration={self.duration}s "
              f"per_iter_timeout={self.per_iter_timeout}s", flush=True)
        print(f"[stress_ocr] baseline: {_format_snapshot(self._baseline)}", flush=True)

        engine = OcrEngine(plugin_id="multi_game_companion_stress")
        if hasattr(engine, "warmup_async"):
            try:
                await engine.warmup_async()
            except Exception as exc:
                print(f"[stress_ocr] ocr warmup failed (non-fatal): {exc}", flush=True)

        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + self.duration

        while not self._stop_event.is_set() and time.monotonic() < deadline:
            iter_start = time.monotonic()

            try:
                # capture 在线程里跑（mss 是阻塞 IO）
                capture_fut = loop.run_in_executor(
                    self._executor, capture_active_frame,
                )
                try:
                    result, path = await asyncio.wait_for(
                        asyncio.shield(capture_fut),
                        timeout=self.per_iter_timeout,
                    )
                except (asyncio.TimeoutError, FutTimeout):
                    self.metrics.add_timeout(self.per_iter_timeout)
                    # 等待 capture 完成（避免线程泄漏）
                    try:
                        await capture_fut
                    except Exception:
                        pass
                    await self._maybe_report()
                    await self._sleep_to_next(deadline)
                    continue

                if not isinstance(result, CaptureResult) or not result.ok or not result.image_base64:
                    self.metrics.add_failure(time.monotonic() - iter_start, "capture_failed")
                    await self._maybe_report()
                    await self._sleep_to_next(deadline)
                    continue

                # OCR
                ocr_fut = loop.run_in_executor(
                    self._executor,
                    lambda b64=result.image_base64: asyncio.run(
                        engine.extract_text_from_base64(b64)
                    ),
                )
                try:
                    text = await asyncio.wait_for(
                        asyncio.shield(ocr_fut),
                        timeout=self.per_iter_timeout,
                    )
                except (asyncio.TimeoutError, FutTimeout):
                    self.metrics.add_timeout(self.per_iter_timeout)
                    self.metrics.add_failure(time.monotonic() - iter_start, "ocr_timeout")
                    try:
                        await ocr_fut
                    except Exception:
                        pass
                    await self._maybe_report()
                    await self._sleep_to_next(deadline)
                    continue

                dt = time.monotonic() - iter_start
                # 隐私：text 不进日志，只记长度
                self.metrics.add_success(dt, capture_ok=result.ok, text_len=len(text) if isinstance(text, str) else 0)

                if self._stop_event.is_set() or time.monotonic() >= deadline:
                    break

                await self._maybe_report()
                await self._sleep_to_next(deadline)

            except Exception as exc:
                self.metrics.add_failure(time.monotonic() - iter_start, f"unexpected: {type(exc).__name__}")
                await self._maybe_report()
                await self._sleep_to_next(deadline)

        # 收尾
        try:
            if hasattr(engine, "close"):
                engine.close()
        except Exception:
            pass
        self._executor.shutdown(wait=True, cancel_futures=True)

        final = self.metrics.summary()
        final["baseline"] = self._baseline
        final["final_system"] = _snapshot()
        final["delta"] = {
            "rss_mb": round(final["final_system"]["rss_mb"] - self._baseline["rss_mb"], 2),
            "threads": final["final_system"]["threads"] - self._baseline["threads"],
            "handles": final["final_system"]["handles"] - self._baseline["handles"],
        }
        return final

    async def _maybe_report(self) -> None:
        now = time.monotonic()
        if now - self._last_report_at < self.report_every:
            return
        self._last_report_at = now
        snap = _snapshot()
        msg = (
            f"[stress_ocr] t={now - self.metrics.started_at:.1f}s "
            f"completed={self.metrics.completed} "
            f"failed={self.metrics.failed} "
            f"timeouts={self.metrics.timeouts} "
            f"rss={snap['rss_mb']}mb threads={snap['threads']} "
            f"handles={snap['handles']}"
        )
        print(msg, flush=True)
        if callable(self.on_report):
            try:
                self.on_report(self.metrics.summary() | snap)
            except Exception:
                pass

    async def _sleep_to_next(self, deadline: float) -> None:
        """睡到下一次 tick（或 deadline，提前返回 if stopped）。"""
        now = time.monotonic()
        sleep_for = min(self.interval, max(0.0, deadline - now))
        if sleep_for <= 0:
            return
        # 用 Event.wait 可中断
        if self._stop_event.wait(timeout=sleep_for):
            return


def _format_snapshot(snap: dict[str, Any]) -> str:
    return f"rss={snap['rss_mb']}mb threads={snap['threads']} handles={snap['handles']}"


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    # Windows GBK 编码兼容
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="OCR 链路压测（L2）")
    parser.add_argument("--duration", type=float, default=60.0,
                        help="总时长（秒），默认 60")
    parser.add_argument("--rate", type=float, default=1.0,
                        help="每秒钟 OCR 次数，默认 1")
    parser.add_argument("--per-iter-timeout", type=float, default=30.0,
                        help="单次 OCR 超时（秒），默认 30")
    parser.add_argument("--report-every", type=float, default=10.0,
                        help="每 N 秒打指标，默认 10")
    parser.add_argument("--report-json", type=str, default=None,
                        help="汇总输出 JSON 路径")
    args = parser.parse_args(argv)

    runner = StressRunner(
        rate=args.rate,
        duration=args.duration,
        per_iter_timeout=args.per_iter_timeout,
        report_every=args.report_every,
    )

    def _sigint(_signum, _frame):
        print("\n[stress_ocr] SIGINT, stopping gracefully...", flush=True)
        runner.stop()
    signal.signal(signal.SIGINT, _sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sigint)

    final = asyncio.run(runner.run())
    print("\n[stress_ocr] FINAL REPORT", flush=True)
    print(json.dumps(final, indent=2, ensure_ascii=False), flush=True)
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps(final, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[stress_ocr] report saved to {args.report_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
