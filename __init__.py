"""multi_game_companion —— 多游戏通用陪聊（L1 MVP）

范围（只做四件事）：
  ① 声明"我现在在玩 XX 游戏"  ② 加载该游戏的术语库
  ③ 陪聊时把术语注入宿主 LLM 上下文（推语境块 + 拉单条）  ④ 切换游戏时换索引

明确不做：游戏检测 / 动作操作 / 决策层 / 主动搭话调度 / 自建 LLM 调用。

分层（业务逻辑在模块里，本文件只做接线与 i18n/Ok-Err 转换）：
    game_registry.py  配置解析：可调项 + 游戏身份 + 别名归一化
    term_store.py     术语库加载：两层合并 + 维度白名单 + 查询索引
    session.py        状态机：当前游戏 / 索引装载卸载 / KV 持久化 / 重推计数
    context_pack.py   产出构造：语境块与查询卡片（硬长度预算）

设计约束（对照 NEKO_KB.txt 第三部分红线，逐条已核）：
  B1  只用 type = "plugin"，不涉及 extension / [plugin.host]
  B2  不使用 Hook 装饰器（当前 SDK 只注册元数据、不执行）
  B3  入口方法名一律不以 "_" 开头 —— 否则 SDK 的 collect_entries 永不收集
  B4  所有入口与工具处理函数均为 async def
  B5  @message 不传 auto_start（当前签名不接受该参数）
  B7  每个入口只用 input_schema（不用 params）；只用 llm_result_fields
  D5  push_message 只用 v2 三件套：parts + visibility + ai_behavior
  D9  不使用 finish()，因此不涉及 reply=/delivery= 之争
  R8  模块顶层无任何副作用（不建连接、不读文件、不改事件循环策略）
  隐私 不读 self.bus；用户原文不进日志、不进返回体；日志只记长度与 game_id
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    message,
    neko_plugin,
    plugin_entry,
    tr,
)

from .context_pack import (
    build_context_block,
    build_lookup_block,
    render_term_card,
    select_context_terms,
)
from .game_registry import GameEntry, GameRegistry, PluginOptions, as_section
from .session import (
    GameSession,
    GameSessionManager,
    ReinjectCounter,
    SessionPersistence,
    SwitchResult,
)
from .term_store import TermEntry, TermLibrary, TermLoadError, load_library


@neko_plugin
class MultiGameCompanionPlugin(NekoPluginBase):
    """多游戏通用陪聊插件。

    职责边界（L1 MVP）：登记当前游戏 → 加载术语库 → 注入语境 → 支持切换。
    不做游戏检测、不做动作操作、不做决策层、不自行调用 LLM。
    """

    def __init__(self, ctx):
        super().__init__(ctx)
        # 全部是"空壳 + 纯对象"，没有任何 IO（R8：auto_start=false 不阻止父进程 import）。
        self._options = PluginOptions()
        self._registry = GameRegistry()
        self._manager = GameSessionManager(
            persistence=SessionPersistence(self.store, logger=self.logger),
            load_library_fn=self._load_library_for,
            options=self._options,
            logger=self.logger,
        )
        self._counter = ReinjectCounter(0)
        # 只保护内存状态；**绝不在持锁期间 await** —— 入口事件循环与 timer 循环
        # 分属不同线程，跨线程锁里 await 会把某个循环线程整条堵死。
        self._state_lock = threading.Lock()
        self._config_ready = False

    # ==================================================================
    # i18n：全部用户可见文案的唯一出口
    # ==================================================================

    def _t(self, message_key: str, **params: object) -> str:
        """渲染一条文案。

        约定：插件内**只有这一处**用变量转发 key；其余所有调用点的第一参数
        必须是字面量。这样"死键/缺键"静态扫描才可靠（见 tests/static）。

        形参刻意叫 ``message_key`` 而不是 ``key``：``PluginI18n.t()`` 的第一个形参
        本身就叫 ``key``，任何 ``{key}`` 占位符都拿不到实参、永远渲染成字面量。
        因此模板里的术语占位符统一叫 ``{term}``（见 i18n/zh-CN.json）。
        """
        return self.i18n.t(message_key, default=message_key, **params)

    # ==================================================================
    # 配置
    # ==================================================================

    async def _reload_config(self) -> Mapping[str, Any]:
        """从宿主读生效配置并重建注册表。失败不抛，退化为默认值。"""
        try:
            raw = await self.config.dump(timeout=5.0)
        except Exception:
            self.logger.warning("multi_game_companion: config dump failed, falling back to defaults")
            raw = {}
        cfg = raw if isinstance(raw, Mapping) else {}

        options = PluginOptions.from_section(as_section(cfg, "multi_game_companion"))
        registry = GameRegistry.from_config(cfg)
        with self._state_lock:
            self._options = options
            self._registry = registry
            self._config_ready = True
        self._counter.reconfigure(options.reinject_every_n_messages)
        self._manager.set_options(options)

        for issue in registry.issues:
            self.logger.warning("multi_game_companion: manifest issue: {}", issue)
        self._ensure_store_enabled(cfg)
        return cfg

    def _ensure_store_enabled(self, cfg: Mapping[str, Any]) -> None:
        """清单声明的 [plugin.store].enabled 在部分宿主上不会预先生效。

        注意：只有配置里确实写了 enabled = true 才补开。配置说 false（或用户
        关掉了）时保持禁用，让上层如实上报 persisted=false，不假装记住了。
        """
        if self.store.enabled:
            return
        store_section = as_section(as_section(cfg, "plugin"), "store")
        if bool(store_section.get("enabled")):
            self.store.enabled = True
            self.logger.info("multi_game_companion: store enabled from effective config")

    async def _ensure_config_ready(self) -> None:
        with self._state_lock:
            ready = self._config_ready
        if not ready:
            await self._reload_config()

    # ==================================================================
    # 生命周期
    # ==================================================================

    @lifecycle(id="startup")
    async def startup(self, **_: Any) -> Any:
        await self._reload_config()
        result = await self._manager.restore(self._registry)
        if result is None:
            self.logger.info("multi_game_companion: started, no current game")
            return Ok({"status": "ready", "game_id": "", "term_count": 0, "notes": []})
        self.logger.info(
            "multi_game_companion: restored game {} ({} terms)",
            result.session.game_id,
            result.library.size(),
        )
        return Ok(
            {
                "status": "ready",
                "game_id": result.session.game_id,
                "display_name": result.session.display_name,
                "term_count": result.library.size(),
                "persisted": result.persisted,
                "notes": self._library_notes(result.library),
            }
        )

    @lifecycle(id="config_change")
    async def config_change(self, **_: Any) -> Any:
        await self._reload_config()
        outcome = self._manager.revalidate(self._registry)
        current = self._manager.current
        self.logger.info("multi_game_companion: config change handled ({})", outcome)
        return Ok(
            {
                "status": "reloaded",
                "outcome": outcome,
                "game_id": current.game_id if current is not None else "",
            }
        )

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any) -> Any:
        self._manager.unload()
        self.logger.info("multi_game_companion: shutdown")
        return Ok({"status": "stopped"})

    # ==================================================================
    # 入口点（Plugin Manager / Agent / 其它插件可调用）
    # ==================================================================
    # 返回值契约：成功 → Ok({..., "summary": <一句话>})；失败 → Err(SdkError(...))。
    # 四个入口都声明了 llm_result_fields=["summary"]，因此每个成功返回体都必须带
    # "summary"，否则结果契约校验会报 LlmResultValidationError。
    # 四个入口都是"即时回执"，按宿主规范显式降级为事件语义（result_kind=event），
    # 避免主 AI 用"任务已完成"的口吻汇报一次查询。

    @plugin_entry(
        id="set_game",
        name=tr("entries.set_game.name", default="登记游戏"),
        description=tr(
            "entries.set_game.description",
            default="登记你当前在玩的游戏，之后陪聊就用那款游戏的语境和术语。",
        ),
        llm_result_fields=["summary"],
        metadata={"result_kind": "event"},
        input_schema={
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": tr(
                        "entries.set_game.param.game",
                        default="游戏名称，例如「原神」「鸣潮」。",
                    ),
                },
            },
            "required": ["game"],
        },
    )
    async def set_game(self, game: str = "", **_: Any) -> Any:
        status, result = await self._register(game)
        if status == "not_found":
            return Err(SdkError(self._t("errors.unknown_game", available=self._registry.available_names())))
        if status == "disabled":
            return Err(SdkError(self._t("errors.game_disabled")))
        if status == "internal":
            return Err(SdkError(self._t("errors.internal")))
        if status in ("terms_dir_missing", "terms_load_failed"):
            return Err(SdkError(self._terms_error_text(status)))
        if result is None:  # pragma: no cover - 上面的分支已覆盖全部非 ok 状态
            return Err(SdkError(self._t("errors.internal")))

        injected = self._push_context(result.session)
        summary = self._t(
            "status.set_game.done",
            game=result.session.display_name,
            count=result.library.size(),
        )
        summary += (
            self._t("status.set_game.injected")
            if injected
            else self._t("status.set_game.not_injected")
        )
        if not result.persisted:
            summary += self._t("errors.store_unavailable")
        return Ok(
            {
                "game_id": result.session.game_id,
                "game": result.session.display_name,
                "term_count": result.library.size(),
                "context_injected": injected,
                "persisted": result.persisted,
                "summary": summary,
            }
        )

    @plugin_entry(
        id="get_current_game",
        name=tr("entries.get_current_game.name", default="查看当前游戏"),
        description=tr(
            "entries.get_current_game.description",
            default="看看现在登记的是哪款游戏。",
        ),
        llm_result_fields=["summary"],
        metadata={"result_kind": "event"},
        input_schema={"type": "object", "properties": {}},
    )
    async def get_current_game(self, **_: Any) -> Any:
        await self._ensure_config_ready()
        session = self._manager.current
        if session is None:
            return Ok({"game_id": "", "game": "", "summary": self._t("status.get_game.none")})
        return Ok(
            {
                "game_id": session.game_id,
                "game": session.display_name,
                "switched_at": session.switched_at,
                "summary": self._t("status.get_game.done", game=session.display_name),
            }
        )

    @plugin_entry(
        id="list_games",
        name=tr("entries.list_games.name", default="列出可用游戏"),
        description=tr(
            "entries.list_games.description",
            default="列出当前已登记、可以陪聊的游戏。",
        ),
        llm_result_fields=["summary"],
        metadata={"result_kind": "event"},
        input_schema={"type": "object", "properties": {}},
    )
    async def list_games(self, **_: Any) -> Any:
        await self._ensure_config_ready()
        games = self._registry.enabled_games()
        if not games:
            return Ok({"games": [], "count": 0, "summary": self._t("status.list_games.empty")})
        current = self._manager.current
        payload = [
            {
                "game_id": entry.game_id,
                "display_name": entry.display_name,
                "current": bool(current is not None and current.game_id == entry.game_id),
            }
            for entry in games
        ]
        names = "、".join(entry.display_name for entry in games)
        return Ok(
            {
                "games": payload,
                "count": len(payload),
                "summary": self._t("status.list_games.done", count=len(payload), games=names),
            }
        )

    @plugin_entry(
        id="refresh_game_context",
        name=tr("entries.refresh_game_context.name", default="重新注入语境"),
        description=tr(
            "entries.refresh_game_context.description",
            default="把当前游戏的语境重新送进对话，长对话里语境变淡时用。",
        ),
        llm_result_fields=["summary"],
        metadata={"result_kind": "event"},
        input_schema={"type": "object", "properties": {}},
    )
    async def refresh_game_context(self, **_: Any) -> Any:
        await self._ensure_config_ready()
        session = self._manager.current
        entry = self._registry.get(session.game_id) if session is not None else None
        if session is None or entry is None:
            return Err(SdkError(self._t("errors.no_game_declared")))
        try:
            # force：用户可能刚改过覆盖层术语，"重新注入语境"要顺带重新读盘。
            self._manager.ensure_library(entry, force=True)
        except TermLoadError as exc:
            return Err(SdkError(self._terms_error_text(exc.code)))
        if not self._push_context(session, announce_switch=False):
            return Err(SdkError(self._t("errors.push_failed")))
        return Ok(
            {
                "game_id": session.game_id,
                "game": session.display_name,
                "context_injected": True,
                "summary": self._t("status.refresh.done", game=session.display_name),
            }
        )

    @plugin_entry(
        id="capture_screen",
        name=tr("entries.capture_screen.name", default="捕获屏幕"),
        description=tr(
            "entries.capture_screen.description",
            default="读取宿主推给模型的最近屏幕帧，用于 OCR 识别。",
        ),
        metadata={"result_kind": "event"},
        input_schema={
            "type": "object",
            "properties": {
                "max_count": {
                    "type": "integer",
                    "description": "最多读取几帧，默认 2，上限 4",
                    "default": 2,
                }
            },
        },
    )
    async def capture_screen(self, *, max_count: int = 2, **_):
        # 只读 frames，不写日志、不外传
        limit = max(1, min(int(max_count), 4))
        try:
            frames = await self.bus.frames.get(max_count=limit)
        except Exception as exc:
            return Err(SdkError(f"bus.frames 不可用: {type(exc).__name__}"))

        if isinstance(frames, Err):
            return frames
        if isinstance(frames, Ok):
            frames = frames.value

        records = list(frames)
        return Ok({
            "count": len(records),
            "frames": [
                {
                    "source": getattr(item, "source", None),
                    "mime": getattr(item, "mime", None),
                    "captured_at": getattr(item, "captured_at", None),
                    "has_image": bool(getattr(item, "image_base64", None)),
                }
                for item in records
            ],
        })

    # ==================================================================
    # 宿主消息：P1 的"每 N 轮重推一次语境"
    # ==================================================================
    # 刻意不看 text / sender 的内容（那是用户对话，属隐私面），只用于计数。
    # 宿主不派发或清空配置时行为是"永不重推"，绝不会刷屏。

    @message(id="on_chat_message", source="chat")
    async def on_chat_message(self, text: str = "", sender: str = "", **_: Any) -> Any:
        del text, sender
        every_n = self._options.reinject_every_n_messages
        if every_n <= 0:
            return Ok({"reinjected": False, "reason": "disabled"})
        session = self._manager.current
        if session is None:
            return Ok({"reinjected": False, "reason": "no_game"})
        if not self._counter.observe(1):
            return Ok(
                {"reinjected": False, "reason": "below_threshold", "pending": self._counter.pending}
            )
        injected = self._push_context(session, announce_switch=False)
        self.logger.info(
            "multi_game_companion: periodic reinject (every {} messages, submitted={})",
            every_n,
            injected,
        )
        return Ok({"reinjected": injected, "game_id": session.game_id})

    # ==================================================================
    # LLM 工具（对话期由模型调用）
    # ==================================================================
    # ⚠️ 与入口点**不同**：@llm_tool 处理函数的返回形状是 main_server 直接读的
    #    JSON —— 规范形状是 {"output": ..., "is_error": bool}，**不是** Ok/Err。
    #    依据：docs/plugins/tool-calling.md 的 "Returning errors"；
    #    参考实现 plugin/plugins/game_agent_minecraft/__init__.py 的 llm_tool 处理器。

    @llm_tool(
        name="set_current_game",
        description=tr(
            "tools.set_current_game.description",
            default="当用户提到自己正在玩某款游戏时调用，登记当前游戏，之后的陪聊会使用该游戏的语境与术语。",
        ),
        parameters={
            "type": "object",
            "properties": {
                "game": {
                    "type": "string",
                    "description": tr(
                        "tools.set_current_game.param.game",
                        default="游戏名称，可以是用户原话里的说法。",
                    ),
                },
            },
            "required": ["game"],
        },
        timeout=10.0,
    )
    async def set_current_game(self, *, game: str = "", **_: Any) -> Any:
        status, result = await self._register(game)
        if status == "ok" and result is not None:
            injected = self._push_context(result.session)
            return {
                "output": {
                    "registered": True,
                    "game": result.session.display_name,
                    "game_id": result.session.game_id,
                    "term_count": result.library.size(),
                    "context_injected": injected,
                    "persisted": result.persisted,
                    "notice": "" if result.persisted else self._t("errors.store_unavailable"),
                },
                "is_error": False,
            }
        if status == "not_found":
            # 用户说了一个库里没有的名字：这是可自纠的正常情况，不是错误。
            return {
                "output": {
                    "registered": False,
                    "reason": "unknown_game",
                    "available": [entry.display_name for entry in self._registry.enabled_games()],
                    "message": self._t(
                        "errors.unknown_game", available=self._registry.available_names()
                    ),
                },
                "is_error": False,
            }
        if status == "disabled":
            return {
                "output": {
                    "registered": False,
                    "reason": "game_disabled",
                    "message": self._t("errors.game_disabled"),
                },
                "is_error": False,
            }
        if status in ("terms_dir_missing", "terms_load_failed"):
            return {
                "output": {
                    "registered": False,
                    "reason": status,
                    "message": self._terms_error_text(status),
                },
                "is_error": True,
                "error": "TERMS_UNAVAILABLE",
            }
        return {
            "output": {
                "registered": False,
                "reason": "internal",
                "message": self._t("errors.internal"),
            },
            "is_error": True,
            "error": "INTERNAL",
        }

    @llm_tool(
        name="lookup_game_term",
        description=tr(
            "tools.lookup_game_term.description",
            default="查询当前游戏术语库里的术语。只有在当前登记的游戏范围内查，查不到就如实说没有，不要用别的游戏的知识代替。",
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": tr(
                        "tools.lookup_game_term.param.query",
                        default="要查的术语或玩家的说法。",
                    ),
                },
            },
            "required": ["query"],
        },
        timeout=10.0,
    )
    async def lookup_game_term(self, *, query: str = "", **_: Any) -> Any:
        await self._ensure_config_ready()
        text = query.strip() if isinstance(query, str) else ""
        # 隐私红线：只记长度，绝不记 query 原文。
        self.logger.info("multi_game_companion: lookup_game_term received query_len={}", len(text))

        session = self._manager.current
        if session is None:
            return {
                "output": {"found": False, "reason": "no_game", "message": self._t("lookup.no_game")},
                "is_error": False,
            }

        library, error_code = self._library_for(session)
        if library is None:
            return {
                "output": {
                    "found": False,
                    "reason": error_code,
                    "message": self._terms_error_text(error_code),
                },
                "is_error": True,
                "error": "TERMS_UNAVAILABLE",
            }

        matches = library.match(text, self._options.lookup_max_matches)
        if not matches:
            self.logger.info(
                "multi_game_companion: lookup_game_term miss (game={}, query_len={})",
                session.game_id,
                len(text),
            )
            return {
                "output": {
                    "found": False,
                    "game": session.display_name,
                    "message": self._t("lookup.not_found", game=session.display_name),
                },
                "is_error": False,
            }

        cards = [
            render_term_card(
                entry,
                entry_line=self._entry_line,
                slang_label=self._t("lookup.slang_label"),
                avoid_label=self._t("lookup.avoid_label"),
                max_chars=self._options.lookup_max_chars_per_term,
            )
            for entry in matches
        ]
        self.logger.info(
            "multi_game_companion: lookup_game_term hit {} (game={}, query_len={})",
            len(matches),
            session.game_id,
            len(text),
        )
        return {
            "output": {
                "found": True,
                "game": session.display_name,
                "count": len(matches),
                "entries": [self._entry_payload(entry) for entry in matches],
                "text": build_lookup_block(
                    self._t("lookup.found_header", count=len(matches)), cards
                ),
            },
            "is_error": False,
        }

    # ==================================================================
    # 内部实现（不以 "_" 开头以外的公开方法一律不挂装饰器）
    # ==================================================================

    def _terms_error_text(self, code: str) -> str:
        """把术语加载错误码映射成文案（错误码不含路径，只含维度/原因）。"""
        if code == "terms_dir_missing":
            return self._t("errors.terms_dir_missing")
        return self._t("errors.terms_load_failed")

    def _warn_dev(self, note: str) -> None:
        self.logger.warning("multi_game_companion: {}", note)

    def _load_library_for(self, entry: GameEntry) -> TermLibrary:
        """默认层读插件目录，覆盖层读 data_path()（KB：不得自拼数据根）。"""
        return load_library(
            default_root=self.plugin_dir,
            override_root=self.data_path(),
            game_id=entry.game_id,
            display_name=entry.display_name,
            terms_dir=entry.terms_dir,
            warn=self._warn_dev,
        )

    def _library_notes(self, library: TermLibrary) -> list[str]:
        notes = [self._t("terms.load.ok", count=library.size())]
        for dimension in library.failed_dimensions:
            notes.append(self._t("terms.load.partial", dimension=dimension))
        for dimension in library.empty_dimensions:
            notes.append(self._t("terms.load.empty_dimension", dimension=dimension))
        return notes

    def _library_for(self, session: GameSession) -> tuple[TermLibrary | None, str]:
        """取当前术语库；不存在时按需重载。

        返回 ``(library, error_code)``：成功时 ``error_code`` 为空串。
        """
        library = self._manager.library
        if library is not None:
            return library, ""
        entry = self._registry.get(session.game_id)
        if entry is None:
            return None, "terms_dir_missing"
        try:
            return self._manager.ensure_library(entry), ""
        except TermLoadError as exc:
            return None, exc.code

    async def _register(self, raw: object) -> tuple[str, SwitchResult | None]:
        """登记/切换的公共路径。返回 ``(status, result)``。

        status ∈ ok / not_found / disabled / terms_dir_missing / terms_load_failed / internal
        """
        await self._ensure_config_ready()
        match = self._registry.resolve(raw)
        if not match.found or match.game is None:
            self.logger.info(
                "multi_game_companion: register miss (input_len={})",
                len(raw) if isinstance(raw, str) else 0,
            )
            return "not_found", None
        entry = match.game
        if not entry.enabled:
            return "disabled", None
        try:
            result = await self._manager.switch(entry)
        except TermLoadError as exc:
            self.logger.warning(
                "multi_game_companion: term library unavailable for game {} ({})",
                entry.game_id,
                exc.code,
            )
            return exc.code, None
        except Exception:
            self.logger.exception("multi_game_companion: unexpected failure while switching game")
            return "internal", None
        # game_id 不是用户隐私，允许进日志（E-21）；用户原文与 query 一律不记。
        self.logger.info(
            "multi_game_companion: registered game {} ({} terms, persisted={})",
            entry.game_id,
            result.library.size(),
            result.persisted,
        )
        return "ok", result

    def _entry_line(self, entry: TermEntry) -> str:
        return self._t("lookup.entry_line", term=entry.key, kind=entry.kind, brief=entry.brief)

    def _entry_payload(self, entry: TermEntry) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": entry.key,
            "kind": entry.kind,
            "brief": entry.brief,
        }
        if entry.aliases:
            payload["aliases"] = list(entry.aliases)
        if entry.slang:
            payload["slang"] = list(entry.slang)
        if entry.avoid:
            payload["avoid"] = entry.avoid
        if entry.source:
            payload["source"] = entry.source
        return payload

    def _push_context(self, session: GameSession, *, announce_switch: bool = True) -> bool:
        """把语境块送进对话上下文。返回是否真的提交成功（失败绝不谎报）。

        ``announce_switch=False`` 用于"重推"（刷新 / 每 N 轮）——那时切换早已发生，
        再报一次"已从 X 切换"只会让模型误以为刚换过游戏。
        """
        library = self._manager.library
        if library is None:
            return False

        entry = self._registry.get(session.game_id)
        selection = select_context_terms(
            library,
            forced_keys=entry.context_terms if entry is not None else (),
            limit=self._options.context_inject_max_terms,
        )
        if selection.missing_keys:
            self.logger.warning(
                "multi_game_companion: context_terms has {} unknown key(s) for game {}",
                len(selection.missing_keys),
                session.game_id,
            )

        switched = (
            announce_switch
            and bool(session.previous_game_id)
            and session.previous_game_id != session.game_id
        )
        if switched:
            previous_entry = self._registry.get(session.previous_game_id)
            previous_name = (
                previous_entry.display_name if previous_entry is not None else session.previous_game_id
            )
            header = self._t(
                "context.title_switched",
                game=session.display_name,
                previous=previous_name,
            )
        else:
            header = self._t("context.title", game=session.display_name)

        block = build_context_block(
            header=header,
            tone_line=self._t("context.tone", tone=library.tone) if library.tone else "",
            terms_header=self._t("context.terms_header"),
            term_lines=[
                self._t("context.term_line", term=item.key, brief=item.brief)
                for item in selection.entries
            ],
            empty_note=self._t("context.no_terms"),
            hint=self._t("context.hint"),
            max_chars=self._options.max_context_chars,
        )

        # push_message v2：只用 parts + visibility + ai_behavior。
        # visibility=[] → 用户看不到原始语境块；ai_behavior="read" → 进模型上下文
        # 但**不**立即触发回复 turn（respond 会造成自我触发）。
        receipt = self.push_message(
            source="multi_game_companion",
            visibility=[],
            ai_behavior="read",
            parts=[{"type": "text", "text": block}],
            priority=0,
            metadata={"game_id": session.game_id, "term_count": len(selection.entries)},
        )
        if isinstance(receipt, Mapping) and receipt.get("submitted") is False:
            self.logger.warning(
                "multi_game_companion: context injection not submitted ({})", receipt.get("reason")
            )
            return False
        return True
