"""工具注册表：自动发现 core/tools 下的只读工具模块并聚合。

约定（每个工具模块必须提供）：
- NAME: str                工具名（= function.name，唯一）
- TOOL_SPEC: dict          OpenAI function calling 规格
- async run(args: dict) -> str   执行，失败返回错误串（不抛异常）
"""

import json

from . import hass, weather, music

# 注册顺序即模型可见顺序；只读工具优先（写操作本期未接入，见 runbook T8.4）
_TOOL_MODULES = [hass, weather, music]

_REGISTRY: dict[str, object] = {
    m.NAME: m for m in _TOOL_MODULES if getattr(m, "NAME", None)
}


def get_all_tools() -> list[dict]:
    """返回聚合后的 OpenAI tools 数组（空数组表示未注册任何工具）。"""
    tools: list[dict] = []
    for m in _TOOL_MODULES:
        spec = getattr(m, "TOOL_SPEC", None)
        if isinstance(spec, dict):
            tools.append(spec)
    return tools


async def execute_tool(name: str, arguments_json: str | None = None) -> str:
    """按名字分发执行一个工具调用。

    arguments_json 可能是 None / 空串 / 合法 JSON 对象串；解析失败时回退空 dict，
    交由具体工具决定如何反应。任何异常都被捕获为错误串返回（不向上抛）。
    """
    mod = _REGISTRY.get(name)
    if mod is None:
        return f"tool_error: unknown tool '{name}'"
    try:
        args: dict = {}
        if arguments_json:
            try:
                parsed = json.loads(arguments_json)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, TypeError):
                return f"tool_error: invalid arguments json for '{name}': {arguments_json!r}"
        return await mod.run(args)
    except Exception as exc:  # 工具内部异常绝不能击穿对话轮
        return f"tool_error: {type(exc).__name__}: {exc}"
