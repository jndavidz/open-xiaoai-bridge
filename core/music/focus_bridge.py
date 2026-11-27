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


class _HoldState:
    """单个渲染端的 focus 持有状态（每设备一把锁，互不牵连）。"""

    def __init__(self):
        self.count = 0
        self.token: FocusToken | None = None
        self.pending: asyncio.Task | None = None
        self.cch_paused = False  # CCH：pause 已发、等待新曲 play 解除（不自动 resume）


# 默认渲染端 = lx06（既有调用无 renderer 参数时走原行为）。
DEFAULT_RENDERER = "lx06"
_holds: dict[str, _HoldState] = {}


def _state(renderer: str) -> _HoldState:
    if renderer not in _holds:
        _holds[renderer] = _HoldState()
    return _holds[renderer]


def enabled() -> bool:
    from core.music import song_bridge

    return song_bridge.managed_enabled()


def manages_music() -> bool:
    """托管模式且当前有在管会话：唤醒/打断不应直接停设备音频。"""

    from core.music import song_bridge

    return song_bridge.managed_enabled() and bool(song_bridge.active_session())


def focus_holding(renderer: str = DEFAULT_RENDERER) -> bool:
    """指定渲染端的 TTS focus 是否持有中（计数/待释放/令牌/CCH pause 任一在即算）。

    供 pcm 编排器判断「暂停是否仍在 TTS 保护下」；focus 状态机的唯一读口，
    外部不要伸手进 _holds 私有状态。
    """

    st = _state(renderer)
    return bool(
        st.count > 0
        or st.token is not None
        or st.pending is not None
        or st.cch_paused
    )


async def acquire(reason: str = "tts", *,
                  renderer: str = DEFAULT_RENDERER) -> FocusToken | None:
    """取指定渲染端的 focus（无托管会话/音乐未在播时返回 None）。

    LX06：走 TtsFocus 令牌（暂停 + 按资格恢复）。
    CCH：无 TtsFocus 令牌，改发 fire-and-forget pause 按住；
        返回一个轻量哨兵令牌，release 时**不** resume（遵守 §2.79：
        解除暂停的唯一正常出口是新曲 play，resume 旧曲会竞速）。
    """

    if renderer == "cch":
        from core.music import cch_cast

        ok, _status = await cch_cast.host_cmd("pause", wait_ack=False)
        if not ok:
            return None
        _state("cch").cch_paused = True
        # 轻量哨兵：session_id 标记设备，release 靠 cch_paused 判断而非真令牌
        return FocusToken("cch-focus", "cch", 0, "cch", 0.0, reason, 0.0)

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
    # CCH 哨兵令牌：不调真 TtsFocus.release（不 resume 旧曲）；清 pause 标记即可。
    if token.session_id == "cch":
        _state("cch").cch_paused = False
        return True, "cch-held-for-new-play"
    from core.music import song_bridge

    focus = song_bridge.focus()
    if focus is None:
        return False, "focus_gone"
    return await focus.release(token)


async def hold(reason: str = "conversation", *, required: bool = False,
               renderer: str = DEFAULT_RENDERER) -> bool:
    """指定渲染端的引用计数持有：第一次持有才真正暂停音乐。

    `required=True` 时暂停确认失败会抛 `FocusDenied`。
    """

    st = _state(renderer)
    if renderer != "cch" and not enabled():
        return False
    if st.pending is not None:
        st.pending.cancel()
        st.pending = None
    if st.count == 0:
        try:
            st.token = await acquire(reason, renderer=renderer)
        except FocusDenied:
            if required:
                raise
            return False
        if st.token is None:
            return False
    st.count += 1
    logger.debug(f"[TtsFocus] hold acquired reason={reason} renderer={renderer} count={st.count}")
    return True


async def release_hold(*, grace_s: float = RELEASE_GRACE_S,
                       renderer: str = DEFAULT_RENDERER) -> None:
    """释放一次持有；归零后**延迟**恢复（去抖）。CCH 不自动 resume。"""

    st = _state(renderer)
    if st.count == 0:
        return
    st.count -= 1
    logger.debug(f"[TtsFocus] hold released renderer={renderer} count={st.count}")
    if st.count > 0:
        return
    st.pending = asyncio.ensure_future(_finish_release(grace_s, renderer))


async def _finish_release(grace_s: float, renderer: str) -> None:
    st = _state(renderer)
    try:
        if grace_s > 0:
            await asyncio.sleep(grace_s)
    except asyncio.CancelledError:
        return
    st.pending = None
    token, st.token = st.token, None
    if token is not None:
        try:
            resumed, why = await release(token)
            if resumed:
                logger.debug(f"[TtsFocus] music resumed after conversation ({why}) renderer={renderer}")
            else:
                logger.debug(f"[TtsFocus] {renderer} focus released ({why})")
        except Exception as exc:  # noqa: BLE001 - 释放失败不得杀会话
            logger.warning(f"[TtsFocus] release failed: {type(exc).__name__}: {exc}")


async def force_release(*, resume: bool = False,
                        renderer: str = DEFAULT_RENDERER) -> None:
    """显式结束 focus（用户「继续播放」/停止）：立即放弃令牌。"""

    st = _state(renderer)
    st.count = 0
    if st.pending is not None:
        st.pending.cancel()
        st.pending = None
    token, st.token = st.token, None
    st.cch_paused = False
    if token is not None and resume:
        await release(token)


def _reset_for_tests() -> None:
    _holds.clear()


def health() -> dict:
    """只读快照：每渲染端的计数/待释放/令牌（供 /api/health 诊断）。"""

    out = {}
    for renderer, st in _holds.items():
        token = st.token
        out[renderer] = {
            "hold_count": st.count,
            "pending_release": st.pending is not None,
            "cch_paused": st.cch_paused,
            "token": None if token is None else {
                "session_id": token.session_id,
                "epoch": token.epoch,
                "provider": token.provider,
            },
        }
    # 兼容旧读法（顶层 hold_count/token = 默认 lx06）
    lx = out.get(DEFAULT_RENDERER, {})
    return {"renderers": out,
            "hold_count": lx.get("hold_count", 0),
            "pending_release": lx.get("pending_release", False),
            "token": lx.get("token")}
