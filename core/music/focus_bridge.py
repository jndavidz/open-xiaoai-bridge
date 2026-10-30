"""生产路径的 TTS focus 接入（B-07，会话级）。

语义（2026-10-29 与用户确认）：

  - **会话级 focus**：进入连续对话就暂停音乐并保持暂停（保护 KWS/VAD 回声），
    对话结束（退出词/超时/停止聆听/显式继续播放）才按资格恢复。
  - **点歌是单轮模式**：起播成功即结束本轮会话，不在对话态等下一句。
  - 播报（announce）与会话共用同一个引用计数：单次播报只短暂持有，不会在
    「人设开场白 → 对话开始」之间把音乐放回来（释放有 2s 去抖，新 hold 会取消）。
  - 用户显式「继续播放」：`force_release()` 立即放弃资格，由命令层驱动恢复。

业务判断（暂停确认、恢复资格）全在 `TtsFocus` 内。
"""

from __future__ import annotations

import asyncio

from core.music.tts_focus import FocusDenied, FocusToken
from core.utils.logger import logger

RELEASE_GRACE_S = 5.0

_hold_count = 0
_hold_token: FocusToken | None = None
_pending_release: asyncio.Task | None = None


def enabled() -> bool:
    from core.music import song_bridge

    return song_bridge.managed_enabled()


def manages_music() -> bool:
    """托管模式且当前有在管会话：唤醒/打断不应直接停设备音频。"""

    from core.music import song_bridge

    return song_bridge.managed_enabled() and bool(song_bridge.active_session())


async def acquire(reason: str = "tts") -> FocusToken | None:
    """直接取一个 focus 令牌（无托管会话/音乐未在播时返回 None）。"""

    if not enabled():
        return None
    from core.music import song_bridge

    focus = song_bridge.focus()
    session_id = song_bridge.active_session()
    if focus is None or not session_id:
        return None
    try:
        return await focus.acquire(session_id, provider=reason)
    except FocusDenied as err:
        if getattr(err, "code", "") in ("not_playing", "stopped"):
            return None
        raise


async def release(token: FocusToken) -> tuple[bool, str]:
    from core.music import song_bridge

    focus = song_bridge.focus()
    if focus is None:
        return False, "focus_gone"
    return await focus.release(token)


async def hold(reason: str = "conversation", *, required: bool = False) -> bool:
    """引用计数持有：第一次持有才真正暂停音乐并把令牌记在桥上。

    返回是否处于「持有中」；`required=True` 时暂停确认失败会抛 `FocusDenied`。
    """

    global _hold_count, _hold_token, _pending_release
    if not enabled():
        return False
    if _pending_release is not None:
        _pending_release.cancel()
        _pending_release = None
    if _hold_count == 0:
        try:
            _hold_token = await acquire(reason)
        except FocusDenied:
            if required:
                raise
            return False
        if _hold_token is None:
            return False
    _hold_count += 1
    logger.debug(f"[TtsFocus] hold acquired reason={reason} count={_hold_count}")
    return True


async def release_hold(*, grace_s: float = RELEASE_GRACE_S) -> None:
    """释放一次持有；归零后**延迟**恢复音乐（去抖：开场白→对话之间的缝隙）。"""

    global _hold_count, _pending_release
    if _hold_count == 0:
        return
    _hold_count -= 1
    logger.debug(f"[TtsFocus] hold released count={_hold_count}")
    if _hold_count > 0:
        return
    _pending_release = asyncio.ensure_future(_finish_release(grace_s))


async def _finish_release(grace_s: float) -> None:
    global _hold_token, _pending_release
    try:
        if grace_s > 0:
            await asyncio.sleep(grace_s)
    except asyncio.CancelledError:
        return
    _pending_release = None
    token, _hold_token = _hold_token, None
    if token is not None:
        try:
            resumed, why = await release(token)
            if resumed:
                logger.debug(f"[TtsFocus] music resumed after conversation ({why})")
            else:
                logger.warning(f"[TtsFocus] music not resumed after conversation ({why})")
        except Exception as exc:  # noqa: BLE001 - 释放失败不得杀会话
            logger.warning(f"[TtsFocus] release failed: {type(exc).__name__}: {exc}")


async def force_release(*, resume: bool = False) -> None:
    """显式结束 focus（用户「继续播放」/停止）：立即放弃令牌。"""

    global _hold_count, _hold_token, _pending_release
    _hold_count = 0
    if _pending_release is not None:
        _pending_release.cancel()
        _pending_release = None
    token, _hold_token = _hold_token, None
    if token is not None and resume:
        await release(token)


def _reset_for_tests() -> None:
    global _hold_count, _hold_token, _pending_release
    _hold_count, _hold_token, _pending_release = 0, None, None


def health() -> dict:
    """只读快照：引用计数/待释放/当前令牌（不暴露设备细节，供 /api/health 诊断）。"""

    token = _hold_token
    return {
        "hold_count": _hold_count,
        "pending_release": _pending_release is not None,
        "token": None if token is None else {
            "session_id": token.session_id,
            "epoch": token.epoch,
            "provider": token.provider,
        },
    }
