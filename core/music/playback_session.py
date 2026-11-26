"""每渲染端独立的播放会话状态机（ADR-0001 L15，2026-11-26 架构收敛）。

一个 renderer 一个实例：状态、身份、观测、受控判据都在实例内，多端天然隔离。
所有改变播放的动作都是状态转换，不是散落的闸门：

    IDLE ──silence()──> SILENCED ──prepare()──> PREPARING ──play()──> PLAYING
                          │                        │                    │
                          └── report(kind) 在 SILENCED/PREPARING 一律 no-op ─┘

不变量（tests/test_playback_session.py 守门）：
1. silence 只发一次 stop；PREPARING 期间保持静默，无旧曲复活。
2. play 以设备 STATE play 新鲜观测裁决（回执 TIMEOUT 不算失败），
   原子提交所有权，单次 renderDisplay，歌词异步写文件。
3. report 是外部事件的唯一入口：非 PLAYING 态一律 no-op——
   一个状态判据取代 FREEZE/TOGGLE/TTS_RESUME/FINISH 四道散点闸门。
"""
from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass

from core.utils.logger import logger


class PlaybackPhase(str, enum.Enum):
    IDLE = "idle"
    SILENCED = "silenced"
    PREPARING = "preparing"
    PLAYING = "playing"


@dataclass
class PreparedPlayback:
    """一次点播 준비 완료后的不可变输入（由 song_request 构造）。"""

    play_url: str
    item: dict
    items: list[dict]
    meta: dict
    bg_url: str
    intent: str


class PlaybackSession:
    """单 renderer 的播放会话（状态机 + 唯一执行点）。"""

    def __init__(self, renderer_id: str):
        self.renderer_id = renderer_id
        self.phase = PlaybackPhase.IDLE
        self.epoch = 0
        self.entry = ""
        self.items: list[dict] = []
        self._recent_freeze: dict[tuple[str, str, int], float] = {}

    def controlled(self) -> bool:
        """SILENCED / PREPARING 期间受控——所有恢复事件应 no-op。"""
        return self.phase in (PlaybackPhase.SILENCED, PlaybackPhase.PREPARING)

    async def silence(self) -> None:
        """IDLE → SILENCED：音频静音一次，旧 meta 保留到新曲覆盖。"""
        from core.music import cch_cast

        if self.phase != PlaybackPhase.IDLE:
            logger.debug(f"[PlaybackSession] silence skipped phase={self.phase.value}",
                         module="PlaybackSession")
            return
        await cch_cast.host_cmd("stop")
        self.phase = PlaybackPhase.SILENCED
        logger.info(f"[PlaybackSession] {self.renderer_id} silenced",
                    module="PlaybackSession")

    def prepare(self) -> None:
        """SILENCED → PREPARING：进入静默等待（TTS/取链由调用方在后台做）。"""
        if self.phase == PlaybackPhase.SILENCED:
            self.phase = PlaybackPhase.PREPARING
            logger.info(f"[PlaybackSession] {self.renderer_id} preparing",
                        module="PlaybackSession")

    async def play(self, prepared: PreparedPlayback) -> bool:
        """PREPARING → PLAYING：起播 + 观测 + 原子提交 + 单次呈现。"""
        from core.music import cch_cast, playback_observation as obs

        if self.phase != PlaybackPhase.PREPARING:
            logger.info(f"[PlaybackSession] play skipped phase={self.phase.value}",
                        module="PlaybackSession")
            return False
        ok, status = await cch_cast.host_cmd("play", url=prepared.play_url)
        confirmed = await obs.observe_play(timeout_s=3.0)
        if not confirmed and not ok:
            logger.warning(f"[PlaybackSession] play failed status={status}",
                           module="PlaybackSession")
            self.phase = PlaybackPhase.IDLE
            return False
        # 原子提交所有权（唯一位置）
        self.epoch += 1
        self.entry = str(prepared.item.get("id") or "")
        self.items = prepared.items
        self._commit_state(prepared)
        # 单次呈现：shield 保证调用方取消时后台仍继续；正常调用等它完成
        presentation = asyncio.create_task(self._present(prepared))
        await asyncio.shield(presentation)
        self.phase = PlaybackPhase.PLAYING
        logger.info(f"[PlaybackSession] {self.renderer_id} playing entry={self.entry}",
                    module="PlaybackSession")
        return True

    def _commit_state(self, prepared: PreparedPlayback) -> None:
        from core.tools import song_request as sr

        sr._cch_state.update({"entry": 0, "items": prepared.items,
                              "no_advance": False, "play_intent": prepared.intent,
                              "played": []})

    async def _present(self, prepared: PreparedPlayback) -> None:
        from core.music import cch_cast, cch_history

        try:
            rd = cch_cast.build_render_data(prepared.play_url, prepared.item,
                                            prepared.meta,
                                            background_image=prepared.bg_url)
            sid = str(prepared.item.get("id") or "")
            if sid:
                rd = cch_cast.inject_lyric(rd, sid)
            await cch_cast.render_display(rd)  # 唯一一次
            await cch_cast.write_lyric(prepared.item)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] present failed: {type(exc).__name__}",
                           module="PlaybackSession")
        try:
            cch_history.note(prepared.items[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] history failed: {type(exc).__name__}",
                           module="PlaybackSession")
        try:
            await cch_cast.push_queue(prepared.items)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] queue failed: {type(exc).__name__}",
                           module="PlaybackSession")

    async def report(self, kind: str, *, ev: dict | None = None) -> dict:
        """外部事件唯一入口：受控态 no-op，PLAYING 态分派到既有执行 seam。"""
        if self.controlled():
            logger.info(f"[PlaybackSession] {self.renderer_id} report suppressed kind={kind}",
                        module="PlaybackSession")
            return {"action": "suppressed", "kind": kind, "renderer": self.renderer_id}
        if self.phase != PlaybackPhase.PLAYING:
            return {"action": "not_playing", "kind": kind, "renderer": self.renderer_id}

        from core.music import cch_np

        if kind == "freeze":
            # 同一 (source, rawID, epoch) 5s 内只恢复一次（跨曲互不抑制）
            import time

            key = (self.entry, self.renderer_id, self.epoch)
            now = time.time()
            if now - self._recent_freeze.get(key, 0.0) < 5.0:
                return {"action": "drop_debounce", "kind": kind,
                        "renderer": self.renderer_id}
            self._recent_freeze[key] = now
            result = await cch_np.replay_current()
        elif kind == "toggle":
            # 保留既有语义：曲终态重播，播放态 M5 原子 toggle（NEED_M5 回退重播）
            from core.music import cch_cast

            st = ((cch_np.snapshot().get("state") or {}).get("extras") or {})
            if str(st.get("statusStr") or "").lower() == "finish":
                result = await cch_np.replay_current()
            else:
                ok, detail = await cch_cast.host_cmd("toggle")
                result = {"action": "toggled", "ok": ok, "detail": detail}
                if not ok and "NEED_M5" in str(detail):
                    result = await cch_np.replay_current()
        elif kind == "tts_resume":
            result = await cch_np.resume_after_tts()
        elif kind == "track_finish":
            result = await cch_np._advance_if_finished()
        else:
            return {"action": "unknown_kind", "kind": kind}
        return dict(result, kind=kind, renderer=self.renderer_id)


_SESSIONS: dict[str, PlaybackSession] = {}


def for_renderer(renderer_id: str = "cch") -> PlaybackSession:
    """按 renderer 取会话——多端隔离（ADR-0001 L15）。"""
    if renderer_id not in _SESSIONS:
        _SESSIONS[renderer_id] = PlaybackSession(renderer_id)
    return _SESSIONS[renderer_id]
