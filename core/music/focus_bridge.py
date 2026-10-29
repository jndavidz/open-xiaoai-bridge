"""生产路径接入 TTS focus 的薄桥（B-07）。

只做三件事：判断托管模式、从运行期取 `TtsFocus`、保证异常时也释放。
业务判断（暂停/确认/恢复资格）全部在 `TtsFocus` 内。

接入点：
  - `speaker.announce`（全部播报入口）：**required** —— 音乐在播但暂停确认不了时
    拒绝播报，绝不让两路音频抢同一个设备；
  - 对话回复（`_speak_response_segments`）：非 required —— focus 失败不阻断回复，
    但取得了就一定释放。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from core.music.tts_focus import FocusDenied, FocusToken


def enabled() -> bool:
    from core.music import song_bridge

    return song_bridge.managed_enabled()


async def acquire(reason: str = "tts") -> FocusToken | None:
    """返回 focus 令牌；无托管会话/音乐未在播时返回 None（无需 focus）。"""

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
        # 音乐已暂停/已停止：不需要也不应该再抢一次 focus
        if getattr(err, "code", "") in ("not_playing", "stopped"):
            return None
        raise


async def release(token: FocusToken) -> tuple[bool, str]:
    from core.music import song_bridge

    focus = song_bridge.focus()
    if focus is None:
        return False, "focus_gone"
    return await focus.release(token)


@asynccontextmanager
async def focus_scope(reason: str, *, required: bool = False):
    token: FocusToken | None = None
    if enabled():
        try:
            token = await acquire(reason)
        except FocusDenied:
            if required:
                raise
            token = None
    try:
        yield token
    finally:
        if token is not None:
            await release(token)
