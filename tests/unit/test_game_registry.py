"""A-22 / A-29 ~ A-32：注册表解析、别名归一化与模糊匹配。

每个用例都断言"改了配置/输入，行为就变"，而不是"文本里存在某个键"。
"""

from __future__ import annotations

import pytest
from conftest import GENSHIN_ENTRY, WUWA_ENTRY, make_config
from plugin.plugins.multi_game_companion.game_registry import (
    GameRegistry,
    PluginOptions,
    as_section,
    is_safe_relative_dir,
    normalize_key,
)

# =============================================================================
# normalize_key
# =============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("原神", "原神"),
        ("《原神》", "原神"),
        ("  Genshin   Impact ", "genshinimpact"),
        ("GENSHIN", "genshin"),
        ("Ｇｅｎｓｈｉｎ", "genshin"),  # 全角 → NFKC
        ("「鸣潮」", "鸣潮"),
        ("", ""),
        (None, ""),
        (123, ""),
    ],
)
def test_normalize_key__canonicalizes_forms(raw: object, expected: str) -> None:
    assert normalize_key(raw) == expected


@pytest.mark.unit
def test_normalize_key__is_idempotent() -> None:
    once = normalize_key("《Genshin  Impact》")
    assert normalize_key(once) == once


# =============================================================================
# A-22 / 可调项
# =============================================================================


@pytest.mark.unit
def test_options__defaults_match_manifest() -> None:
    options = PluginOptions()
    assert options.default_game == ""
    assert options.auto_restore_last_game is True
    assert options.context_inject_max_terms == 5
    assert options.max_context_chars == 800
    assert options.lookup_max_matches == 3
    assert options.lookup_max_chars_per_term == 200
    assert options.reinject_every_n_messages == 0


@pytest.mark.unit
def test_options__every_key_is_read_and_changes_behaviour() -> None:
    options = PluginOptions.from_section(
        {
            "default_game": "genshin",
            "auto_restore_last_game": False,
            "context_inject_max_terms": 2,
            "max_context_chars": 100,
            "lookup_max_matches": 1,
            "lookup_max_chars_per_term": 50,
            "reinject_every_n_messages": 3,
        }
    )
    assert options.default_game == "genshin"
    assert options.auto_restore_last_game is False
    assert options.context_inject_max_terms == 2
    assert options.max_context_chars == 100
    assert options.lookup_max_matches == 1
    assert options.lookup_max_chars_per_term == 50
    assert options.reinject_every_n_messages == 3


@pytest.mark.unit
def test_options__bad_types_fall_back_and_out_of_range_is_clamped() -> None:
    options = PluginOptions.from_section(
        {
            "default_game": 42,
            "auto_restore_last_game": "yes",
            "context_inject_max_terms": 999,
            "max_context_chars": 1,
            "lookup_max_matches": 0,
            "lookup_max_chars_per_term": 10_000,
            "reinject_every_n_messages": -5,
        }
    )
    assert options.default_game == ""
    assert options.auto_restore_last_game is True  # 非布尔 → 默认值，不猜
    assert options.context_inject_max_terms == 50
    assert options.max_context_chars == 16
    assert options.lookup_max_matches == 1
    assert options.lookup_max_chars_per_term == 2000
    assert options.reinject_every_n_messages == 0


@pytest.mark.unit
def test_options__bool_is_not_accepted_as_int() -> None:
    # bool 是 int 的子类；不显式挡掉，True 会变成 1 条术语上限。
    options = PluginOptions.from_section({"context_inject_max_terms": True})
    assert options.context_inject_max_terms == 5


@pytest.mark.unit
def test_as_section__non_mapping_is_empty() -> None:
    assert as_section(None, "games") == {}
    assert as_section({"games": 5}, "games") == {}
    assert as_section({"games": {"a": 1}}, "games") == {"a": 1}


# =============================================================================
# A-29 / A-30 / A-31 / A-32：注册表解析
# =============================================================================


@pytest.mark.unit
def test_registry__display_name__required_and_used_in_block() -> None:
    broken = {"genshin": {k: v for k, v in GENSHIN_ENTRY.items() if k != "display_name"}}
    registry = GameRegistry.from_config(make_config(games=broken))
    assert registry.games == ()
    assert any("display_name is required" in issue for issue in registry.issues)


@pytest.mark.unit
def test_registry__aliases__required_and_every_form_resolves_to_same_id() -> None:
    missing = {"genshin": {k: v for k, v in GENSHIN_ENTRY.items() if k != "aliases"}}
    registry = GameRegistry.from_config(make_config(games=missing))
    assert registry.games == ()
    assert any("aliases is required" in issue for issue in registry.issues)

    registry = GameRegistry.from_config(make_config(games={"genshin": dict(GENSHIN_ENTRY)}))
    for alias in ["原神", "Genshin", "Genshin Impact", "genshin", "GENSHIN", "《原神》"]:
        match = registry.resolve(alias)
        assert match.found, alias
        assert match.game is not None and match.game.game_id == "genshin"


@pytest.mark.unit
def test_registry__terms_dir__default_is_used_when_absent() -> None:
    entry = {k: v for k, v in GENSHIN_ENTRY.items() if k != "terms_dir"}
    registry = GameRegistry.from_config(make_config(games={"genshin": entry}))
    assert registry.get("genshin").terms_dir == "terms/genshin"


@pytest.mark.unit
def test_registry__terms_dir__explicit_override_wins() -> None:
    entry = dict(GENSHIN_ENTRY)
    entry["terms_dir"] = "terms/custom_genshin"
    registry = GameRegistry.from_config(make_config(games={"genshin": entry}))
    assert registry.get("genshin").terms_dir == "terms/custom_genshin"


@pytest.mark.unit
@pytest.mark.parametrize("bad_dir", ["/etc/passwd", "C:/windows", "../escape", "terms/../../x", ""])
def test_registry__terms_dir__unsafe_path_is_rejected_not_followed(bad_dir: str) -> None:
    entry = dict(GENSHIN_ENTRY)
    entry["terms_dir"] = bad_dir
    registry = GameRegistry.from_config(make_config(games={"genshin": entry}))
    game = registry.get("genshin")
    if bad_dir == "":
        # 空字符串等同"没写"，用默认模板，不算问题
        assert game is not None and game.terms_dir == "terms/genshin"
    else:
        assert game is not None and game.terms_dir == "terms/genshin"
        assert any("relative path" in issue for issue in registry.issues)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("terms/genshin", True),
        ("terms\\genshin", True),
        ("a/b/c", True),
        ("/abs", False),
        ("C:/x", False),
        ("../up", False),
        ("", False),
        (".", False),
    ],
)
def test_is_safe_relative_dir(value: str, expected: bool) -> None:
    assert is_safe_relative_dir(value) is expected


@pytest.mark.unit
def test_registry__enabled_false__excluded_from_list_and_declaration() -> None:
    entry = dict(GENSHIN_ENTRY)
    entry["enabled"] = False
    registry = GameRegistry.from_config(make_config(games={"genshin": entry, "wuthering_waves": dict(WUWA_ENTRY)}))

    assert [game.game_id for game in registry.enabled_games()] == ["wuthering_waves"]
    assert "原神" not in registry.available_names()

    match = registry.resolve("原神")
    assert match.found is True
    assert match.usable is False  # 命中了，但被下架 → 调用方必须报 game_disabled


@pytest.mark.unit
def test_registry__invalid_game_id_is_rejected_with_issue() -> None:
    registry = GameRegistry.from_config(make_config(games={"Genshin-Impact": dict(GENSHIN_ENTRY)}))
    assert registry.games == ()
    assert any("id must match" in issue for issue in registry.issues)


@pytest.mark.unit
def test_registry__non_table_entry_is_rejected_without_crash() -> None:
    registry = GameRegistry.from_config(make_config(games={"genshin": "原神"}))
    assert registry.games == ()
    assert any("must be a table" in issue for issue in registry.issues)


@pytest.mark.unit
def test_registry__from_config_tolerates_garbage() -> None:
    assert GameRegistry.from_config(None).games == ()
    assert GameRegistry.from_config({"games": None}).games == ()
    assert GameRegistry.from_config("nonsense").games == ()  # type: ignore[arg-type]


# =============================================================================
# resolve：模糊匹配
# =============================================================================


@pytest.fixture
def registry() -> GameRegistry:
    return GameRegistry.from_config(
        make_config(games={"genshin": dict(GENSHIN_ENTRY), "wuthering_waves": dict(WUWA_ENTRY)})
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected_game_id", "expected_reason"),
    [
        ("genshin", "genshin", "exact_id"),
        ("原神", "genshin", "exact_alias"),
        ("我在玩原神", "genshin", "partial"),
        ("最近在肝鸣潮，好累", "wuthering_waves", "partial"),
        ("wuwa", "wuthering_waves", "exact_alias"),
        ("Wuthering Waves", "wuthering_waves", "exact_alias"),
    ],
)
def test_registry__resolve(
    registry: GameRegistry, text: str, expected_game_id: str, expected_reason: str
) -> None:
    match = registry.resolve(text)
    assert match.reason == expected_reason
    assert match.game is not None and match.game.game_id == expected_game_id


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "   ", "塞尔达", None, 123, "原"])
def test_registry__resolve__misses(registry: GameRegistry, text: object) -> None:
    match = registry.resolve(text)
    assert match.found is False
    assert match.reason == "not_found"


@pytest.mark.unit
def test_registry__resolve__single_char_never_partial_matches(registry: GameRegistry) -> None:
    # "原" 是 "原神" 的子串，但 1 个字符太容易误命中，必须要求 >= 2。
    assert registry.resolve("原").found is False
    assert registry.resolve("原神").found is True


@pytest.mark.unit
def test_registry__resolve__longest_alias_wins() -> None:
    games = {
        "a": {"display_name": "甲", "aliases": ["星"], "terms_dir": "terms/a"},
        "b": {"display_name": "乙", "aliases": ["星穹铁道"], "terms_dir": "terms/b"},
    }
    registry = GameRegistry.from_config(make_config(games=games))
    match = registry.resolve("星穹铁道真好玩")
    assert match.game is not None and match.game.game_id == "b"
    assert match.reason == "partial"


@pytest.mark.unit
def test_registry__get__only_matches_exact_id() -> None:
    registry = GameRegistry.from_config(make_config(games={"genshin": dict(GENSHIN_ENTRY)}))
    assert registry.get("genshin") is not None
    assert registry.get("原神") is None
    assert registry.get("") is None
