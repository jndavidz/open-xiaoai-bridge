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
    """一次点播准备完成后的不可变输入（由 song_request 构造）。"""

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
        self._last_toggle_ts: float = 0.0
        self._silence_ts: float = 0.0  # 静音起点：观测只认此后的 play
        self._inflight: asyncio.Task | None = None  # 在途恢复任务（新意图到达时取消）
        self.intent_seq = 0  # 意图代次：新 silence 递增，旧 play 观测后 CAS 失败即作废

    def cancel_inflight(self) -> None:
        """作废上一曲的在途恢复（FREEZE/换代取票可能跨几十秒）。"""
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()
            logger.info("[PlaybackSession] cancelled inflight recovery")
        self._inflight = None

    def observation_since(self) -> float:
        """供换代/重播的观测裁决使用（排除上一首遗留 STATE play）。"""
        return self._silence_ts

    def controlled(self) -> bool:
        """受控态（事务中 / 对话 focus 持有中）——所有恢复事件应 no-op。"""
        if self.phase in (PlaybackPhase.SILENCED, PlaybackPhase.PREPARING):
            return True
        try:
            from core.music import focus_bridge

            return focus_bridge.focus_holding()
        except Exception:  # noqa: BLE001 - 读不到按受控处理（fail-closed）
            return True

    async def silence(self) -> None:
        """任意态 → SILENCED：新意图抢占时也要静音（PLAYING→SILENCED）。

        真机 11:21:56「play skipped phase=playing」就是上一曲停在 PLAYING，
        新点播无法进入 PREPARING。新意图到达即从当前态重置，音频 stop 一次。
        """
        from core.music import cch_cast

        self.cancel_inflight()  # 新意图到达：先废掉旧曲在途恢复
        self.intent_seq += 1  # 递增意图代次 ⇒ 上一曲 play 观测后 CAS 失败，不覆盖新意图
        # 用户裁决（2026-11-26）：唤醒词静默 = AGMC pause（audioStatus:9，实例保留），
        # 保持暂停态直到 bridge 发来新曲 play；不用 stop（audioStatus:8 杀实例）。
        # 抢占 pause = fire-and-forget：真机默认等回执+重发 = 11s，旧曲持续响。
        # 抢占要最快生效；广播偶丢由后续 play(新曲) 覆盖兜底，不靠回执重发保证送达。
        ok, status = await cch_cast.host_cmd("pause", wait_ack=False)
        if not ok and "NEED_M5" in str(status):
            # 实例已死（曲终/被抢）：pause 无处可施，补一个 stop 清场防旧曲残影
            await cch_cast.host_cmd("stop", wait_ack=False)
        self.phase = PlaybackPhase.SILENCED
        self._recent_freeze.clear()  # 新会话：清掉上一曲的去抖桶
        self._last_toggle_ts = 0.0
        import time

        self._silence_ts = time.time()
        logger.info(f"[PlaybackSession] {self.renderer_id} silenced "
                    f"(from prior phase)")

    def mark_playing(self, prepared: PreparedPlayback) -> None:
        """活实例轨：设备已由 cast_native 起播，只提交状态（不二次 stop/play）。"""
        self.epoch += 1
        self.entry = str(prepared.item.get("id") or "")
        self.items = prepared.items
        self._commit_state(prepared)
        self.phase = PlaybackPhase.PLAYING
        asyncio.create_task(self._present(prepared))
        logger.info(f"[PlaybackSession] {self.renderer_id} adopted playing "
                    f"entry={self.entry}")

    def prepare(self) -> None:
        """SILENCED/PLAYING → PREPARING：进入静默等待（换代/新点播都经此）。"""
        if self.phase in (PlaybackPhase.SILENCED, PlaybackPhase.PLAYING):
            self.phase = PlaybackPhase.PREPARING
            logger.info(f"[PlaybackSession] {self.renderer_id} preparing",
                        )

    async def play(self, prepared: PreparedPlayback) -> bool:
        """PREPARING → PLAYING：起播 + 观测 + 原子提交 + 单次呈现。"""
        from core.music import cch_cast, playback_observation as obs

        if self.phase != PlaybackPhase.PREPARING:
            logger.info(f"[PlaybackSession] play skipped phase={self.phase.value}",
                        )
            return False
        my_seq = self.intent_seq  # 代次快照：观测期间若新意图到达则 seq 变，作废本次提交
        ok, status = await cch_cast.host_cmd(
            "play", url=prepared.play_url, retry_once=False)
        # 显示层立即投：不等 18s 观测窗（map.md ab13cb1 历史教训——架构收敛时
        # 把 render 挪到 observe_play 之后，真机显示层滞后 10s）。音轨已由 hostcmd
        # 起播，显示层与实际音轨同源，观测只用于所有权提交裁决。
        await self._render_display(prepared)
        confirmed = await obs.observe_play(since_ts=self.observation_since())
        if my_seq != self.intent_seq:
            logger.info("[PlaybackSession] play superseded by newer intent (seq changed)")
            return False
        if not confirmed and not ok:
            logger.warning(f"[PlaybackSession] play failed status={status}",
                           )
            self.phase = PlaybackPhase.IDLE
            return False
        # 原子提交所有权（唯一位置）
        self.epoch += 1
        self.entry = str(prepared.item.get("id") or "")
        self.items = prepared.items
        self._commit_state(prepared)
        # 单次呈现：shield 保证调用方取消时后台仍继续；正常调用等它完成
        presentation = asyncio.create_task(self._present(prepared))
        try:
            await asyncio.shield(presentation)
        except asyncio.CancelledError:
            self.phase = PlaybackPhase.IDLE  # 取消不留半状态（后台呈现仍继续）
            raise
        self.phase = PlaybackPhase.PLAYING
        logger.info(f"[PlaybackSession] {self.renderer_id} playing entry={self.entry}")
        return True

    def _commit_state(self, prepared: PreparedPlayback) -> None:
        from core.tools import song_request as sr

        sr._cch_state.update({"entry": 0, "items": prepared.items,
                              "no_advance": False, "play_intent": prepared.intent,
                              "played": []})

    async def _render_display(self, prepared: PreparedPlayback) -> None:
        """显示层首帧（含歌词 URL 纯函数注入）——hostcmd play 后立即投，不等观测。"""
        from core.music import cch_cast

        try:
            rd = cch_cast.build_render_data(prepared.play_url, prepared.item,
                                            prepared.meta,
                                            background_image=prepared.bg_url)
            sid = str(prepared.item.get("id") or "")
            if sid:
                rd = cch_cast.inject_lyric(rd, sid)
            await cch_cast.render_display(rd)  # 全程唯一一次
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] render failed: {type(exc).__name__}",
                           )

    async def _present(self, prepared: PreparedPlayback) -> None:
        from core.music import cch_cast, cch_history

        try:
            await cch_cast.write_lyric(prepared.item)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] present failed: {type(exc).__name__}",
                           )
        try:
            cch_history.note(prepared.items[0])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] history failed: {type(exc).__name__}",
                           )
        try:
            await cch_cast.push_queue(prepared.items)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[PlaybackSession] queue failed: {type(exc).__name__}",
                           )

    def dispatch(self, kind: str) -> asyncio.Task | None:
        """异步入口：登记在途恢复任务（新意图可取消）。返回 task 供测试/调用方。"""
        if self._inflight is not None and not self._inflight.done():
            return self._inflight  # 已有在途恢复，不重复启动
        self._inflight = asyncio.create_task(self.report(kind))
        return self._inflight

    async def report(self, kind: str, *, ev: dict | None = None) -> dict:
        """外部事件唯一入口：受控态 no-op，PLAYING 态分派到既有执行 seam。"""
        if self.controlled():
            logger.info(f"[PlaybackSession] {self.renderer_id} report suppressed kind={kind}",
                        )
            return {"action": "suppressed", "kind": kind, "renderer": self.renderer_id}
        if self.phase != PlaybackPhase.PLAYING:
            return {"action": "not_playing", "kind": kind, "renderer": self.renderer_id}

        # 冻结/恢复需重新 stop+play：先重置到 PREPARING（一次 stop + 静默持有）。
        # 换代是自然曲终后的推进（旧曲已停），不额外 silence——cch_np 内部
        # 已完成取票+cast，成功后把 session 推回 PLAYING。
        if kind in ("freeze", "tts_resume"):
            await self.silence()
            self.prepare()

        from core.music import cch_np

        if kind == "track_finish":
            result = await cch_np._advance_if_finished()
            if str(result.get("action") or "") == "advanced":
                self.phase = PlaybackPhase.PLAYING
            return dict(result, kind=kind, renderer=self.renderer_id)

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
            self.phase = PlaybackPhase.PLAYING  # 恢复成功回到可换代态
        elif kind == "toggle":
            # 换代 stop 的 pushNotify 副产品会被 tap_hook 当 PLAYTOGGLE 上报
            # （真机 11:20:49 advance 后立刻 replay）——5s 去抖挡住二次启动
            import time

            now = time.time()
            if now - self._last_toggle_ts < 5.0:
                return {"action": "drop_debounce", "kind": kind,
                        "renderer": self.renderer_id}
            self._last_toggle_ts = now
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
            self.phase = PlaybackPhase.PLAYING  # 恢复成功回到可换代态
        else:
            return {"action": "unknown_kind", "kind": kind}
        return dict(result, kind=kind, renderer=self.renderer_id)


_SESSIONS: dict[str, PlaybackSession] = {}


def for_renderer(renderer_id: str = "cch") -> PlaybackSession:
    """按 renderer 取会话——多端隔离（ADR-0001 L15）。"""
    if renderer_id not in _SESSIONS:
        _SESSIONS[renderer_id] = PlaybackSession(renderer_id)
    return _SESSIONS[renderer_id]
