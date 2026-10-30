"""工具：红外发射（A 级写操作，A-7/P3 票 04——首个写操作工具实战）。

分级：RISK = "a"（2026-10-30 用户裁决：**家电控制属常规操作，不需要确认**；
只有危险/非常规操作才走 C 级二次确认）。执行前仍会播报复述（announce 双闸门），
但**不等应答**——复述即执行。不可逆/异常操作请自行归入 C 级（打印、外发、
摄像头快照等），本工具不属于那一类。

码值库外部提供：`ir.codes` 配置表（device.action → µs 序列，热重载）。
LLM 只传 device/action，不直接编 µs 序列（防止模型幻觉出任意红外码）；
码值表未配置的动作一律拒绝。learn 录制属设备写、单独授权，本工具不含。

边界（关中断纪律，registry ir_send notes）：串行锁 + 发送期间不与
播放/TTS 并发（管线 speaking 态拒绝，避免音频 XRUN）。
"""

import asyncio

from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.utils import pipeline as conversation_pipeline

NAME = "ir_send"

# P3 票 03：风险分级声明（a=执行前复述、复述即执行；家电控制不需二次确认）
RISK = "a"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "通过小爱音箱发射红外信号（家电控制），直接执行、不需要二次确认。"
            "device+action 必须是已配置的红外码值（如 ac/on、fan/off），"
            "未配置的动作会被拒绝。调用前会复述待执行动作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": "设备名（ir.codes 配置表的第一段，如 ac、fan、tv）",
                },
                "action": {
                    "type": "string",
                    "description": "动作名（ir.codes 配置表的第二段，如 on、off）",
                },
            },
            "required": ["device", "action"],
        },
    },
}

# 串行锁：内核发送期间关中断，一次只能发一条（模块级单例）
_SERIAL = asyncio.Lock()


def _code_table() -> dict:
    table = ConfigManager.instance().get_app_config("ir.codes", {})
    return table if isinstance(table, dict) else {}


def lookup_code(device: str, action: str) -> str | None:
    """查码值表：`"<device>/<action>"` → µs 序列。"""
    key = f"{str(device or '').strip()}/{str(action or '').strip()}"
    pattern = _code_table().get(key)
    return str(pattern).strip() if isinstance(pattern, str) and pattern.strip() else None


async def run(args: dict) -> str:
    device = str(args.get("device") or "").strip()
    action = str(args.get("action") or "").strip()
    if not device or not action:
        return "请提供设备名和动作（如 ac/on）"

    pattern = lookup_code(device, action)
    if pattern is None:
        available = ", ".join(sorted(_code_table())) or "（码值表为空）"
        return f"没有配置 {device}/{action} 的红外码值；已配置：{available}"

    # 不与播放并发：speaking 态拒绝（关中断会打断音频流）
    if conversation_pipeline.state().value == "speaking":
        return "正在播放音频，暂不发射红外（发送期间会打断音频），请稍后再试"

    from core.device import get_device

    try:
        async with _SERIAL:
            res = await get_device().ir_send(pattern)
    except ValueError as exc:
        return f"已拒绝：{exc}"
    except Exception as exc:  # noqa: BLE001 - 工具异常收敛为错误串
        logger.error(f"[tools/ir_send] {type(exc).__name__}: {exc}")
        return f"红外发射失败：{exc}"

    if not res.ok:
        return f"红外发射失败：{res.error}"
    return f"已发射红外：{device}/{action}"
