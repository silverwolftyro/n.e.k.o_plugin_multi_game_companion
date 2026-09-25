"""游戏注册表与插件选项（身份真源）。

把宿主下发的生效配置解析成结构化对象：
  - ``[multi_game_companion]`` → :class:`PluginOptions`（可调项，全部有范围钳制）
  - ``[games.<game_id>]``      → :class:`GameRegistry`（身份，术语文件之前就要能解析）

设计约定（与 plugin.toml 自定义段注释一致）：
  - 身份字段（game_id / display_name / aliases / terms_dir / enabled / context_terms）
    只在本段声明；术语包里的 ``core.toml [game]`` 只承载内容（tone / notes）。
  - 本模块**没有任何 IO**：配置由 ``self.config.dump()`` 下发，这里只做纯解析与匹配，
    因此可以脱离宿主单测。

隐私约定：本模块不持有、不返回、不记录任何用户原文。用户输入只经过
:func:`normalize_key` 变成临时匹配键，调用方只允许记录"是否命中"与键长。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 未显式声明 ``terms_dir`` 时使用的相对路径模板（相对 plugin_dir）。
TERMS_DIR_TEMPLATE = "terms/{game_id}"

#: 部分匹配（子串/前缀）的最短归一化键长。1 个字符的输入太容易误命中。
_MIN_PARTIAL_LEN = 2

_WS_RE = re.compile(r"[\s\u3000]+")
_GAME_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# 玩家常把游戏名写在书名号/引号里，归一化时统一剥掉两端包裹符。
_TRIM_CHARS = "《》「」『』\"'“”‘’()（）[]【】<>"


def normalize_key(text: object) -> str:
    """把一段文本归一化成可比较的键：NFKC + casefold + 去空白 + 剥两端包裹符。

    归一化是幂等的，且不含任何隐式截断——调用方可以安全地拿它做字典键。
    """
    if not isinstance(text, str):
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _WS_RE.sub("", folded)
    return folded.strip(_TRIM_CHARS)


def as_section(container: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    """安全取出一个 TOML 表；不是表就返回空表（绝不抛）。"""
    if not isinstance(container, Mapping):
        return {}
    value = container.get(name)
    return value if isinstance(value, Mapping) else {}


def _clean_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_str_list(value: object) -> tuple[str, ...]:
    """把 TOML 值规范成字符串元组：接受字符串（视作单元素）与字符串列表。"""
    if isinstance(value, str):
        items: Sequence[object] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = value
    else:
        return ()
    out: list[str] = []
    for item in items:
        text = item.strip() if isinstance(item, str) else ""
        if text and text not in out:
            out.append(text)
    return tuple(out)


def is_safe_relative_dir(value: str) -> bool:
    """``terms_dir`` 只允许 plugin_dir 下的普通相对路径（拒绝绝对路径与 ``..``）。"""
    if not value:
        return False
    if value.startswith(("/", "\\")) or ":" in value:
        return False
    parts = [part for part in re.split(r"[\\/]+", value) if part not in ("", ".")]
    return bool(parts) and all(part != ".." for part in parts)


def _clamp_int(value: object, *, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


def _as_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


# =============================================================================
# 插件可调项
# =============================================================================


@dataclass(frozen=True)
class PluginOptions:
    """``[multi_game_companion]`` 的解析结果。

    每个字段都有硬范围：清单里写错值不会让插件崩，也不会让预算失控。
    """

    default_game: str = ""
    auto_restore_last_game: bool = True
    context_inject_max_terms: int = 5
    max_context_chars: int = 800
    lookup_max_matches: int = 3
    lookup_max_chars_per_term: int = 200
    reinject_every_n_messages: int = 0

    @classmethod
    def from_section(cls, section: Mapping[str, Any] | None) -> PluginOptions:
        data = section if isinstance(section, Mapping) else {}
        return cls(
            default_game=_clean_str(data.get("default_game")),
            auto_restore_last_game=_as_bool(data.get("auto_restore_last_game"), True),
            context_inject_max_terms=_clamp_int(
                data.get("context_inject_max_terms"), low=0, high=50, default=5
            ),
            max_context_chars=_clamp_int(
                data.get("max_context_chars"), low=16, high=8192, default=800
            ),
            lookup_max_matches=_clamp_int(
                data.get("lookup_max_matches"), low=1, high=10, default=3
            ),
            lookup_max_chars_per_term=_clamp_int(
                data.get("lookup_max_chars_per_term"), low=40, high=2000, default=200
            ),
            reinject_every_n_messages=_clamp_int(
                data.get("reinject_every_n_messages"), low=0, high=500, default=0
            ),
        )


# =============================================================================
# 游戏身份
# =============================================================================


@dataclass(frozen=True)
class GameEntry:
    """一个已登记游戏的**身份**（不含任何术语内容）。"""

    game_id: str
    display_name: str
    aliases: tuple[str, ...]
    terms_dir: str
    enabled: bool = True
    context_terms: tuple[str, ...] = ()

    def normalized_keys(self) -> tuple[str, ...]:
        """本游戏全部可用于匹配的归一化键（id + 正式名 + 别名），去重保序。"""
        keys: list[str] = []
        for raw in (self.game_id, self.display_name, *self.aliases):
            key = normalize_key(raw)
            if key and key not in keys:
                keys.append(key)
        return tuple(keys)


@dataclass(frozen=True)
class GameMatch:
    """一次声明的解析结果。

    ``game`` 是命中的条目（可能 ``enabled = False``，由调用方判定）；
    ``reason`` 用于日志，不含用户原文。
    """

    game: GameEntry | None
    reason: str
    matched_len: int = 0

    @property
    def found(self) -> bool:
        return self.game is not None

    @property
    def usable(self) -> bool:
        return self.game is not None and self.game.enabled


@dataclass(frozen=True)
class GameRegistry:
    """``[games.*]`` 的解析结果。不可变，可安全跨线程共享。"""

    games: tuple[GameEntry, ...] = ()
    issues: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> GameRegistry:
        section = as_section(config, "games")
        games: list[GameEntry] = []
        issues: list[str] = []

        for raw_game_id, raw_entry in section.items():
            game_id = str(raw_game_id).strip()
            if not _GAME_ID_RE.match(game_id):
                issues.append(f"games.{game_id}: id must match ^[a-z][a-z0-9_]*$")
                continue
            if not isinstance(raw_entry, Mapping):
                issues.append(f"games.{game_id}: entry must be a table")
                continue

            display_name = _clean_str(raw_entry.get("display_name"))
            if not display_name:
                issues.append(f"games.{game_id}: display_name is required")
                continue

            aliases = _clean_str_list(raw_entry.get("aliases"))
            if not aliases:
                issues.append(f"games.{game_id}: aliases is required and must be non-empty")
                continue

            declared_dir = _clean_str(raw_entry.get("terms_dir"))
            if declared_dir and not is_safe_relative_dir(declared_dir):
                issues.append(f"games.{game_id}: terms_dir must be a relative path inside plugin_dir")
                declared_dir = ""

            games.append(
                GameEntry(
                    game_id=game_id,
                    display_name=display_name,
                    aliases=aliases,
                    terms_dir=declared_dir or TERMS_DIR_TEMPLATE.format(game_id=game_id),
                    enabled=_as_bool(raw_entry.get("enabled"), True),
                    context_terms=_clean_str_list(raw_entry.get("context_terms")),
                )
            )

        return cls(games=tuple(games), issues=tuple(issues))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, game_id: object) -> GameEntry | None:
        wanted = str(game_id or "").strip()
        if not wanted:
            return None
        for game in self.games:
            if game.game_id == wanted:
                return game
        return None

    def enabled_games(self) -> tuple[GameEntry, ...]:
        return tuple(game for game in self.games if game.enabled)

    def available_names(self) -> str:
        """给错误文案用的"当前可用"列表；不包含被下架的游戏。"""
        return "、".join(game.display_name for game in self.enabled_games())

    def resolve(self, text: object) -> GameMatch:
        """把用户说出的游戏名解析成注册条目。

        顺序：精确 game_id → 精确别名 → 最长部分匹配。部分匹配要求
        归一化后至少 2 个字符，避免单字误命中。
        """
        query = normalize_key(text)
        if not query:
            return GameMatch(None, "not_found")

        for game in self.games:
            if query == normalize_key(game.game_id):
                return GameMatch(game, "exact_id", len(query))

        for game in self.games:
            if query in game.normalized_keys():
                return GameMatch(game, "exact_alias", len(query))

        if len(query) >= _MIN_PARTIAL_LEN:
            best_game: GameEntry | None = None
            best_score: tuple[int, int] | None = None
            for index, game in enumerate(self.games):
                for key in game.normalized_keys():
                    if len(key) < _MIN_PARTIAL_LEN:
                        continue
                    if query in key or key in query:
                        score = (len(key), -index)
                        if best_score is None or score > best_score:
                            best_score, best_game = score, game
            if best_game is not None:
                return GameMatch(best_game, "partial", len(query))

        return GameMatch(None, "not_found")


__all__ = [
    "TERMS_DIR_TEMPLATE",
    "GameEntry",
    "GameMatch",
    "GameRegistry",
    "PluginOptions",
    "as_section",
    "is_safe_relative_dir",
    "normalize_key",
]
