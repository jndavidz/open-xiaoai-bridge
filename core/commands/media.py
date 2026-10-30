"""对话轮内的媒体命令识别：连续对话裸说「继续播放/暂停/停止播放/下一首/上一首」。

背景（真机 2026-10-30）：连续对话进行中，不带唤醒词说媒体命令没有反应——对话
期间 KWS 已暂停（`wakeup_session._start_backend_conversation` 进门即 pause），
这句话只能走本轮 ASR → LLM，模型口头答应、设备不动；再喊一次唤醒词才生效
（KWS 命中走 `route_before_wakeup` 指令表）。修法：ASR 定稿后、发给 LLM 之前
在**本轮内**识别并直接执行。

分工（不另造一套）：

  - 识别：本模块 —— 整句归一化精确命中（复用 `core/commands/matching` 的
    B-31 口径：全角→半角、去空白标点、拉丁小写）。**不做子串/模糊**：
    对话态宁可少截，吞掉普通聊天比漏一条命令更糟。
  - 执行：`core/tools/song_request.run({"action": ...})` —— 与唤醒词路径、
    LLM 工具调用完全同一条 action 分发（托管模式内部转
    `core.music.song_bridge.command`）。

边界：

  - 只在**媒体相关状态**下拦截：托管会话在管（`song_bridge.active_session()`，
    含暂停态）或旧内存队列有当前曲（`song_request.has_queue()`）。没有音乐时
    「继续播放」照常走 LLM 聊天，不吞普通对话。
  - 「停止」只在媒体态才认；`exit_keywords` 不含「停止」（真机 2026-10-28），
    无音乐时它仍是普通聊天词，不会误退出会话。
  - 唤醒词路径（KWS 命中后的 `route_before_wakeup`）不经过本模块，行为不变。
"""

from __future__ import annotations

from core.commands.matching import resolve_command_key
from core.utils.logger import logger

LOG_MODULE = "Commands"

# 归一化整句 → song_request action（值必须是 validate() 接受的 action）。
# 键取自 deploy/config.py 指令表实际词形（KWS 唤醒路径的同名词）+ 口语常见说法。
MEDIA_COMMAND_ACTIONS: dict[str, str] = {
    # 继续播放
    "继续播放": "resume",
    "继续放音乐": "resume",
    "开始播放": "resume",
    "恢复播放": "resume",
    "播放": "resume",
    "继续": "resume",
    # 暂停（音乐保持静默，会话继续）
    "暂停": "pause",
    "暂停一下": "pause",
    "暂停播放": "pause",
    "暂停音乐": "pause",
    "先暂停一下": "pause",
    # 停止播放
    "停止播放": "stop",
    "停止音乐": "stop",
    "停止播放音乐": "stop",
    "停止": "stop",
    "停下": "stop",
    # 下一首
    "下一首": "next",
    "下一曲": "next",
    "下一首歌曲": "next",
    "切换下一首": "next",
    "切歌": "next",
    "换一首": "next",
    "换首歌": "next",
    # 上一首
    "上一首": "prev",
    "上一曲": "prev",
    "上一首歌曲": "prev",
    "切换上一首": "prev",
    "回到上一首": "prev",
}

# 交接音频的动作：执行后设备侧已有声音（继续/切歌）或队列已清（停止）——
# 对话循环必须单轮收束，不得继续听（否则会把音乐当说话喂回 VAD/ASR）。
_AUDIO_HANDOVER_ACTIONS = frozenset({"resume", "next", "prev", "stop"})


def match_media_command(text) -> str | None:
    """整句是否媒体命令；命中返回 song_request action，否则 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    key = resolve_command_key(text, MEDIA_COMMAND_ACTIONS)
    return MEDIA_COMMAND_ACTIONS.get(key) if key else None


def hands_over_audio(action) -> bool:
    """该动作是否已把音频所有权交出去（= 必须结束聆听会话）。"""
    return str(action or "") in _AUDIO_HANDOVER_ACTIONS


def media_active() -> bool:
    """当前是否媒体相关状态（在播或暂停）：决定要不要把媒体命令截下来。

    托管模式不维护旧 `_state`（队列归 store），旧模式不建托管会话——两边
    都查，任一为真即是媒体态。
    """
    try:
        from core.music import song_bridge

        if song_bridge.active_session():
            return True
    except Exception as exc:  # noqa: BLE001 - 托管面不可用不阻断旧队列判定
        logger.debug(f"[commands] managed media state unavailable: {exc}")
    try:
        from core.tools import song_request

        return bool(song_request.has_queue())
    except Exception as exc:  # noqa: BLE001 - 状态查询失败按「无媒体」处理（不截）
        logger.warning(f"[commands] legacy media state unavailable: {exc}")
        return False


async def execute_media_command(action: str) -> str:
    """执行媒体命令（复用 song_request 的 action 分发，失败收敛为文本）。"""
    from core.tools import registry, song_request

    # 每轮按命令自身结果判定（不沿用上一轮工具留下的免播标记）
    registry.reset_reply_suppression()
    try:
        result = await song_request.run({"action": action})
    except Exception as exc:  # noqa: BLE001 - 工具异常绝不能击穿对话轮
        logger.warning(f"[commands] media command {action} failed: {exc}")
        return f"媒体命令没能执行（{action}）"
    logger.info(f"[commands] media command in conversation: {action} -> {result!r}")
    return result
