"""工具：查询音箱自身运行状态（只读，A-5/P3 票 01）。

数据源 = 设备能力层只读白名单行（播放上下文 / 播放状态 / 麦克风 / 内存 /
夜间模式），不触任何凭证面。米家周边设备的发现通道（`_local.miot_device_scan`）
经事实层判不可达（ws: docs/devices/lx06/miot-ubus-surface.md §5，device_unprov
态 -30013 offline），本工具只覆盖音箱自身；家庭其他智能设备的状态查询走
`hass_get_states` 工具。
"""

from core.ref import get_speaker
from core.utils.logger import logger

NAME = "device_status"

# P3 票 03：风险分级声明（read=只读无确认；分级语义见 guardrails）
RISK = "read"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "查询小爱音箱自身的运行状态（只读）：正在播放什么、音量、"
            "麦克风开关、剩余内存、夜间模式是否生效。"
            "用于回答『音箱现在在放什么』『家里音箱什么状态』；"
            "其他智能家居设备（灯/空调等）请改用 hass_get_states 工具。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _memory_mb(free: object) -> str:
    if isinstance(free, (int, float)) and not isinstance(free, bool):
        return f"{free / 1024 / 1024:.1f} MB"
    return "未知"


def _play_label(play_state: object, context_status: object) -> str:
    """播放状态中文化：优先 playback_status 的英文枚举，回落 context 状态码。"""
    mapping = {"playing": "播放中", "paused": "暂停", "idle": "空闲"}
    if isinstance(play_state, str) and play_state in mapping:
        return mapping[play_state]
    if play_state is not None:
        return str(play_state)
    # player_get_context.info.status：1=播放中 / 2=暂停（设备真值）
    return {1: "播放中", 2: "暂停"}.get(context_status, f"状态码 {context_status}")


async def run(args: dict) -> str:
    speaker = get_speaker()
    if speaker is None:
        return "设备层不可用（speaker 未就绪）"
    try:
        device = speaker.device
        context = await device.player_context()
        info = context.readback or {}
        playback = await device.playback_status()
        play_state = (playback.readback or {}).get("status")
        mic = await device.mic_get()
        mic_state = (mic.readback or {}).get("mic")
        memory = await device.system_memory()
        free = (memory.readback or {}).get("free")
        night = await device.nightmode_current()
        night_volume = (night.readback or {}).get("volume")

        lines = [
            f"播放：{_play_label(play_state, info.get('status'))}",
            f"音量：{info.get('volume', '未知')}",
            f"麦克风：{'开' if mic_state == 'on' else '关' if mic_state == 'off' else '未知'}",
            f"剩余内存：{_memory_mb(free)}",
            "夜间模式："
            + ("已生效（音量允许调小）" if night_volume == "night"
               else "未生效" if night_volume == "normal" else "未知"),
        ]
        return "；".join(lines)
    except Exception as exc:  # 工具内部异常绝不能击穿对话轮
        logger.error(f"[tools/device_status] {type(exc).__name__}: {exc}")
        return f"音箱状态查询失败：{exc}"
