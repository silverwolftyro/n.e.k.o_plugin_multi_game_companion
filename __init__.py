"""multi_game_companion —— 多游戏通用陪聊（L1 MVP）

本文件是**接口声明桩**：只声明插件类、生命周期、入口点与 LLM 工具，
函数体返回 Err(SdkError("not implemented"))，不含任何业务逻辑。

业务实现将在以下模块中落地（本阶段刻意未创建）：
    game_registry.py  游戏注册表解析、别名归一化
    term_store.py     术语库加载 / 双层合并 / 别名索引
    session.py        当前游戏状态机与持久化（self.store）
    context_pack.py   语境块与查询返回体的构造（含长度上限）

范围边界（L1 MVP）：登记当前游戏 → 加载术语库 → 注入语境 → 切换游戏。
不做游戏检测、不做动作操作、不做决策层、不自行调用 LLM。

设计约束（对照 NEKO_KB.txt 第三部分红线，逐条已核）：
  B1  只用 type = "plugin"，不涉及 extension / [plugin.host]
  B2  不使用 Hook 装饰器（当前 SDK 只注册元数据、不执行）
  B3  入口方法名一律不以 "_" 开头 —— 否则 SDK 的 collect_entries 永不收集
  B4  所有入口与工具处理函数均为 async def
  B5  不使用 @message（若将来使用，不得传 auto_start）
  B7  每个入口只用 input_schema（不用 params）；只用 llm_result_fields
  R8  模块顶层无任何副作用（不建连接、不读文件、不改事件循环策略）
"""

from __future__ import annotations

from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    tr,
)

# 桩阶段统一的失败原因。刻意用普通字符串而非 i18n 键：
# 这是开发者占位文案，不是给用户看的成品文案。
_NOT_IMPLEMENTED = "not implemented"


@neko_plugin
class MultiGameCompanionPlugin(NekoPluginBase):
    """多游戏通用陪聊插件。

    职责边界（L1 MVP）：登记当前游戏 → 加载术语库 → 注入语境 → 支持切换。
    不做游戏检测、不做动作操作、不做决策层、不自行调用 LLM。
    """

    # ==================================================================
    # 生命周期
    # ==================================================================
    # 这里刻意返回 Ok 而不是 Err，这不是"未实现"，而是**完整实现**：
    # 本插件在启动/关闭时没有任何资源需要建立或释放。
    # 若在此返回 Err，宿主会把启动判定为降级/失败，插件每次启动都报错——
    # 而事实只是业务逻辑还没写。红线检查只关心 @lifecycle(id=...) 是否存在
    # （validate_cmd.py:798/800 的 AST 检查），与该函数的返回值无关。

    @lifecycle(id="startup")
    async def startup(self, **_: Any) -> Any:
        return Ok({"status": "ready"})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any) -> Any:
        return Ok({"status": "stopped"})

    # ==================================================================
    # 入口点（Plugin Manager / Agent / 其它插件可调用）
    # ==================================================================
    # 返回值契约：成功 → Ok({..., "summary": <一句话>})；失败 → Err(SdkError(...))。
    # 四个入口都声明了 llm_result_fields=["summary"]，因此**实现时每个成功返回体
    # 都必须带 "summary" 字段**，否则结果契约校验会报 LlmResultValidationError。

    @plugin_entry(
        id="set_game",
        name=tr("entries.set_game.name", default="登记游戏"),
        description=tr(
            "entries.set_game.description",
            default="登记你当前在玩的游戏，之后陪聊就用那款游戏的语境和术语。",
        ),
        llm_result_fields=["summary"],
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
    async def set_game(self, game: str, **_: Any) -> Any:
        return Err(SdkError(_NOT_IMPLEMENTED))

    @plugin_entry(
        id="get_current_game",
        name=tr("entries.get_current_game.name", default="查看当前游戏"),
        description=tr(
            "entries.get_current_game.description",
            default="看看现在登记的是哪款游戏。",
        ),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
    )
    async def get_current_game(self, **_: Any) -> Any:
        return Err(SdkError(_NOT_IMPLEMENTED))

    @plugin_entry(
        id="list_games",
        name=tr("entries.list_games.name", default="列出可用游戏"),
        description=tr(
            "entries.list_games.description",
            default="列出当前已登记、可以陪聊的游戏。",
        ),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
    )
    async def list_games(self, **_: Any) -> Any:
        return Err(SdkError(_NOT_IMPLEMENTED))

    @plugin_entry(
        id="refresh_game_context",
        name=tr("entries.refresh_game_context.name", default="重新注入语境"),
        description=tr(
            "entries.refresh_game_context.description",
            default="把当前游戏的语境重新送进对话，长对话里语境变淡时用。",
        ),
        llm_result_fields=["summary"],
        input_schema={"type": "object", "properties": {}},
    )
    async def refresh_game_context(self, **_: Any) -> Any:
        return Err(SdkError(_NOT_IMPLEMENTED))

    # ==================================================================
    # LLM 工具（对话期由模型调用）
    # ==================================================================
    # ⚠️ 与入口点**不同**：@llm_tool 处理函数的最终返回形状是「普通 dict」，
    #    或错误信封 {"output": ..., "is_error": True, "error": "..."} ——
    #    不是 Ok/Err。
    #    依据：docs/plugins/tool-calling.md 的 "Returning errors" 一节；
    #          参考实现 plugin/plugins/sts2_autoplay/catgirl_llm.py 的调用点
    #          （llm_get_status 返回的是普通 dict，而 plugin_entry 走 _run_entry 的 Ok/Err）。
    #    桩阶段统一返回 Err：SDK 会把处理函数包成保留前缀的动态入口，模型侧收到
    #    "工具调用失败"——这正是桩阶段想要的行为。**业务落地时这两个函数必须
    #    改成返回 dict。**

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
        # 实现要点（业务阶段）：
        #   - 别名归一化失败 → 返回 {"output": {"found": False, "available": [...]}, "is_error": False}
        #   - 登记成功后推语境块（push_message v2：parts + visibility=[] + ai_behavior="read"）
        return Err(SdkError(_NOT_IMPLEMENTED))

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
        # 实现要点（业务阶段）：
        #   - 只在当前游戏的索引里查；跨游戏必须返回 found: False + 当前游戏提示
        #   - 返回条数 ≤ lookup_max_matches，单条 ≤ lookup_max_chars_per_term
        #   - 日志只记 len(query)，绝不记 query 原文（隐私红线）
        return Err(SdkError(_NOT_IMPLEMENTED))
