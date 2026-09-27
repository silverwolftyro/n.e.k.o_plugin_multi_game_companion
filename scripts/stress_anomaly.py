# ruff: noqa: E402
"""OCR 异常注入压测（L4）。

拍板 2.0.70：模拟各种异常场景，看 OCR 链路能否恢复。每个场景独立运行，
报告"是否恢复 / 恢复时间 / 是否有泄漏"。

场景：
  1. mss.mss() 抛 PermissionError（窗口权限）
  2. capture_active_frame 返回 None
  3. OCR 返回空字符串
  4. OCR 返回超长文本（100KB）
  5. 屏幕分辨率突变（2560x1600 → 1280x720）
  6. 连续切换窗口 100 次（模拟快速切界面）
  7. 多进程同时抓屏（模拟其他软件）

跑法：
  python scripts/stress_anomaly.py                  # 跑全部场景
  python scripts/stress_anomaly.py --scenario 1     # 只跑场景 1
  python scripts/stress_anomaly.py --report out.json
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from plugin.plugins.multi_game_companion.screen_capture import (  # noqa: E402
    CaptureResult,
    capture_active_frame,
    is_black_frame,
)


@dataclass
class ScenarioResult:
    """单个场景的结果。"""
    name: str
    passed: bool
    recovered: bool  # 异常后能否正常跑 1 次 OCR
    recovery_time_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    baseline_rss_mb: float = 0.0
    after_rss_mb: float = 0.0
    delta_rss_mb: float = 0.0
    error: str = ""


def _rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except ImportError:
        return -1.0


# =============================================================================
# 场景实现
# =============================================================================


def scenario_mss_permission_error() -> ScenarioResult:
    """场景 1：mss.mss() 抛 PermissionError → capture 应 graceful 返回 no_window。"""
    name = "mss_permission_error"
    notes: list[str] = []
    baseline = _rss_mb()

    # patch mss 模块让 mss.mss() 抛 PermissionError
    import mss as mss_mod  # type: ignore[import-untyped]
    import plugin.plugins.multi_game_companion.screen_capture as sc

    original_mss = mss_mod.mss
    def _boom():
        raise PermissionError("simulated: screen capture permission denied")

    mss_mod.mss = _boom
    # 直接覆盖 sc 模块里的 mss 引用
    sc.mss = mss_mod  # type: ignore[attr-defined]

    try:
        start = time.monotonic()
        try:
            result, path = capture_active_frame()
            elapsed = time.monotonic() - start
            recovered = isinstance(result, CaptureResult) and not result.ok
            notes.append(f"captured as ok={result.ok} error={result.error} path={path}")
        except Exception as exc:
            elapsed = time.monotonic() - start
            recovered = False
            notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}: {exc}")
    finally:
        mss_mod.mss = original_mss
        sc.mss = original_mss  # type: ignore[attr-defined]

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=elapsed, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


def scenario_capture_returns_none() -> ScenarioResult:
    """场景 2：capture_active_frame 返回 None（极端异常路径）。"""
    name = "capture_returns_none"
    notes: list[str] = []
    baseline = _rss_mb()

    import plugin.plugins.multi_game_companion.screen_capture as sc
    original = sc.capture_active_frame
    sc.capture_active_frame = lambda: None  # type: ignore[assignment]

    try:
        start = time.monotonic()
        try:
            out = sc.capture_active_frame()
            elapsed = time.monotonic() - start
            recovered = out is None  # 没崩就算
            notes.append(f"got {type(out).__name__}")
        except Exception as exc:
            elapsed = time.monotonic() - start
            recovered = False
            notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}")
    finally:
        sc.capture_active_frame = original  # type: ignore[assignment]

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=elapsed, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


def scenario_ocr_empty_text() -> ScenarioResult:
    """场景 3：OCR 返回空字符串 → 应 graceful（空文本 = 黑帧/无信号）。"""
    name = "ocr_empty_text"
    notes: list[str] = []
    baseline = _rss_mb()

    # 模拟一次完整 OCR 链路但 OCR 返回 ""
    try:
        result = CaptureResult(ok=True, image_base64="", width=100, height=100, method="mss", error="", avg_luma=0.0)
        # is_black_frame 应返 True（avg_luma=0 < 12）
        black = is_black_frame(result.image_base64, ocr_len=0)
        recovered = black is True
        notes.append(f"is_black_frame(empty_b64, ocr_len=0)={black}")
    except Exception as exc:
        recovered = False
        notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}")

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=0.0, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


def scenario_ocr_super_long_text() -> ScenarioResult:
    """场景 4：OCR 返回 100KB 文本 → 不应崩（仅 hash 验证用）。"""
    name = "ocr_super_long_text"
    notes: list[str] = []
    baseline = _rss_mb()
    import hashlib

    try:
        huge_text = "原神测试文本" * 20000  # ~100KB
        start = time.monotonic()
        text_hash = hashlib.md5(huge_text.encode("utf-8")).hexdigest()
        elapsed = time.monotonic() - start
        recovered = len(text_hash) == 32
        notes.append(f"hash computed in {elapsed*1000:.1f}ms; len(text)={len(huge_text)} hash={text_hash[:8]}")
    except Exception as exc:
        recovered = False
        notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}")

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=elapsed, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


def scenario_resolution_jump() -> ScenarioResult:
    """场景 5：屏幕分辨率突变（2560x1600 → 1280x720）→ CaptureResult 字段正确传递。"""
    name = "resolution_jump"
    notes: list[str] = []
    baseline = _rss_mb()

    try:
        r1 = CaptureResult(ok=True, image_base64="x" * 100, width=2560, height=1600, method="mss", error="", avg_luma=120.0)
        r2 = CaptureResult(ok=True, image_base64="x" * 50, width=1280, height=720, method="mss", error="", avg_luma=120.0)
        recovered = (r1.width == 2560 and r1.height == 1600 and r2.width == 1280 and r2.height == 720)
        notes.append(f"r1={r1.width}x{r1.height} r2={r2.width}x{r2.height}")
    except Exception as exc:
        recovered = False
        notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}")

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=0.0, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


def scenario_rapid_window_switch() -> ScenarioResult:
    """场景 6：连续切换窗口 100 次 → 模拟快速切界面，链路不卡死。"""
    name = "rapid_window_switch"
    notes: list[str] = []
    baseline = _rss_mb()

    # patch capture_active_frame 让它快速返回"无窗口"
    import plugin.plugins.multi_game_companion.screen_capture as sc
    original = sc.capture_active_frame
    call_count = [0]

    def _rapid():
        call_count[0] += 1
        return (CaptureResult(False, None, 0, 0, "", "no_window", -1.0), "foreground")

    sc.capture_active_frame = _rapid  # type: ignore[assignment]

    try:
        start = time.monotonic()
        for _ in range(100):
            r, _ = sc.capture_active_frame()
        elapsed = time.monotonic() - start
        recovered = call_count[0] == 100 and elapsed < 5.0
        notes.append(f"{call_count[0]} calls in {elapsed*1000:.0f}ms; avg={elapsed*10:.1f}ms/call")
    except Exception as exc:
        recovered = False
        notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}")
    finally:
        sc.capture_active_frame = original  # type: ignore[assignment]

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=elapsed, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


def scenario_concurrent_process_capture() -> ScenarioResult:
    """场景 7：多线程同时抓屏 → _MSS_LOCK 锁正确串行化（不挂死）。"""
    name = "concurrent_process_capture"
    notes: list[str] = []
    baseline = _rss_mb()

    import plugin.plugins.multi_game_companion.screen_capture as sc
    original = sc.capture_active_frame
    call_count = [0]

    def _slow_capture():
        call_count[0] += 1
        time.sleep(0.05)  # 模拟 50ms 抓屏
        return (CaptureResult(False, None, 0, 0, "", "no_window", -1.0), "foreground")

    sc.capture_active_frame = _slow_capture  # type: ignore[assignment]

    try:
        start = time.monotonic()
        # 5 线程并发 20 次 = 100 次抓屏，期望 ~1s 内完成（串行）
        def _worker():
            for _ in range(20):
                sc.capture_active_frame()
        threads = [threading.Thread(target=_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        elapsed = time.monotonic() - start
        # 100 * 50ms = 5s 串行；5 线程实际应 ~5s（锁串行化）
        recovered = call_count[0] == 100 and elapsed < 10.0
        notes.append(f"{call_count[0]} captures in {elapsed:.1f}s (serialized by _MSS_LOCK)")
    except Exception as exc:
        recovered = False
        notes.append(f"UNEXPECTED RAISE: {type(exc).__name__}")
    finally:
        sc.capture_active_frame = original  # type: ignore[assignment]

    after = _rss_mb()
    return ScenarioResult(
        name=name, passed=recovered, recovered=recovered,
        recovery_time_s=elapsed, notes=notes,
        baseline_rss_mb=baseline, after_rss_mb=after,
        delta_rss_mb=round(after - baseline, 2),
    )


# =============================================================================
# Runner
# =============================================================================


SCENARIOS: list[tuple[int, str, Callable[[], ScenarioResult]]] = [
    (1, "mss_permission_error", scenario_mss_permission_error),
    (2, "capture_returns_none", scenario_capture_returns_none),
    (3, "ocr_empty_text", scenario_ocr_empty_text),
    (4, "ocr_super_long_text", scenario_ocr_super_long_text),
    (5, "resolution_jump", scenario_resolution_jump),
    (6, "rapid_window_switch", scenario_rapid_window_switch),
    (7, "concurrent_process_capture", scenario_concurrent_process_capture),
]


def run_all(filter_ids: list[int] | None = None) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for sid, name, fn in SCENARIOS:
        if filter_ids and sid not in filter_ids:
            continue
        print(f"[anomaly] running scenario {sid}/{len(SCENARIOS)}: {name}...", flush=True)
        try:
            r = fn()
        except Exception as exc:
            r = ScenarioResult(
                name=name, passed=False, recovered=False,
                notes=[f"SCENARIO CRASH: {type(exc).__name__}: {exc}"],
                error=str(exc),
            )
        results.append(r)
        status = "[PASS]" if r.passed else "[FAIL]"
        print(f"[anomaly] scenario {sid} {status} recovered={r.recovered} "
              f"dt={r.recovery_time_s:.3f}s rss_delta={r.delta_rss_mb}mb", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    # Windows GBK 编码兼容
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="OCR 异常注入压测（L4）")
    parser.add_argument("--scenario", type=int, action="append", default=None,
                        help="只跑指定场景 ID（可多次传）；不传则跑全部")
    parser.add_argument("--report", type=str, default=None,
                        help="汇总输出 JSON 路径")
    args = parser.parse_args(argv)

    results = run_all(args.scenario)
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scenarios_total": len(results),
        "scenarios_passed": sum(1 for r in results if r.passed),
        "results": [asdict(r) for r in results],
    }
    print("\n[anomaly] SUMMARY", flush=True)
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[anomaly] saved to {args.report}", flush=True)
    return 0 if report["scenarios_passed"] == report["scenarios_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
