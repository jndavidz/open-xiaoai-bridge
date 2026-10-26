"""工具注册表：自动发现 core/tools 下的工具模块并聚合。

约定（每个工具模块必须提供）：
- NAME: str                工具名（= function.name，唯一）
- TOOL_SPEC: dict          OpenAI function calling 规格
- RISK: str                风险分级（P3 票 03）：read / a / b / c / d
                           ——未声明或非法的模块**拒绝注册**（保守缺省）
- async run(args: dict) -> str   执行，失败返回错误串（不抛异常）
- validate(args) -> str | None   （可选，B 级）白名单/数值范围校验，
                           返回拒绝原因即拒绝执行

分级语义见 core/tools/guardrails.py（A–D 四分级 + 二次确认）：
D 级工具对模型不可见（get_all_tools 过滤）且 execute_tool 拒绝留证。
"""

import json

from core.utils.logger import logger

from . import device_status, hass, ir_send, music, song_request, weather
from .guardrails import ConfirmationManager, execute_guarded, module_risk

# 注册顺序即模型可见顺序；只读工具优先，C 级写工具（红外）经护栏注册
_TOOL_MODULES = [hass, weather, music, device_status, ir_send, song_request]

# 注册校验（P3 票 03）：未声明合法 RISK 的模块拒绝注册——写工具不声明
# 分级就无法过闸门，杜绝「无分级写工具」静默上线。
_REGISTRY: dict[str, object] = {}
for _m in _TOOL_MODULES:
    if getattr(_m, "NAME", None) and module_risk(_m) is not None:
        _REGISTRY[_m.NAME] = _m
    else:
        logger.error(
            f"tool module {getattr(_m, '__name__', _m)} rejected (missing/invalid RISK)",
            module="Tools",
        )

# B/C 级共享的确认状态机（P3 票 04 对话层接线）：execute_tool 的首次
# 调用登记 pending 并返回复述请求；用户应答由对话循环经
# guardrails.handle_confirmation_utterance 拦截执行。
_CONFIRMATIONS = ConfirmationManager()


def confirmation_manager() -> ConfirmationManager:
    """共享确认状态机（对话层拦截应答用；测试重置换新实例）。"""
    return _CONFIRMATIONS


# 出公网的工具（用量记账「联网次数」口径，P2 ticket 03）：hass/music 走
# 局域网（HA / LMS），不计联网；song_request 搜歌走 kw-qq-api（票 01）计入。
EXTERNAL_NETWORK_TOOLS = frozenset({"weather_get", "song_request"})


def is_external_network_tool(name: object) -> bool:
    return str(name or "") in EXTERNAL_NETWORK_TOOLS


def get_all_tools() -> list[dict]:
    """返回聚合后的 OpenAI tools 数组（空数组表示未注册任何工具）。

    D 级（永不开放）工具对模型不可见。
    """
    tools: list[dict] = []
    for m in _REGISTRY.values():
        if module_risk(m) == "d":
            continue
        spec = getattr(m, "TOOL_SPEC", None)
        if isinstance(spec, dict):
            tools.append(spec)
    return tools


async def execute_tool(name: str, arguments_json: str | None = None) -> str:
    """按名字分发执行一个工具调用。

    arguments_json 可能是 None / 空串 / 合法 JSON 对象串；解析失败时回退空 dict，
    交由具体工具决定如何反应。任何异常都被捕获为错误串返回（不向上抛）。
    D 级调用直接拒绝并留证（纵深防御——模型本就看不到它们）。
    B/C 级（P3 票 04 对话层接线）：经 execute_guarded——首次调用登记
    pending 并返回「需要确认」的复述请求（由模型转述播报）；用户应答
    由对话循环在 ASR 定稿处拦截（guardrails.handle_confirmation_utterance）。
    """
    mod = _REGISTRY.get(name)
    if mod is None:
        return f"tool_error: unknown tool '{name}'"
    risk = module_risk(mod)
    if risk == "d":
        logger.warning(
            f"tool {name} is risk-D; direct call refused", module="Guardrails"
        )
        return "该操作属于永不开放的危险类别（D 级），已拒绝并记录"
    try:
        args: dict = {}
        if arguments_json:
            try:
                parsed = json.loads(arguments_json)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, TypeError):
                return f"tool_error: invalid arguments json for '{name}': {arguments_json!r}"
        if risk in ("b", "c"):
            return await execute_guarded(
                mod,
                args,
                action_id=f"{name}:latest",
                confirmation=_CONFIRMATIONS,
            )
        return await mod.run(args)
    except Exception as exc:  # 工具内部异常绝不能击穿对话轮
        return f"tool_error: {type(exc).__name__}: {exc}"
