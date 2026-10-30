"""TTS focus 与音乐恢复资格（B-07）。

不变量：

  1. **先暂停音乐并确认**，再播报；暂停未确认就不播。
  2. 恢复资格绑定 `session/epoch/entry/focus token`：用户 pause/stop/next、外部接管、
     代次变化、重复播报换令牌，任一发生即作废，旧令牌永远换不回播放。
  3. 暂停/停止态**不偷偷 seek/Play**；backend 未声明 resume 时如实返回未恢复。
  4. 每个 provider 独立给结果（ok / failed / cancelled / unknown / weak），
     不用一个布尔成功把四个 provider 合并；弱完成/未知先做一次安全停止再决定。
  5. 麦态/KWS/录制的归还在调用方 finally 里（本模块不接管这些资源）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from core.music.model import Intent, ManagedError, Phase, SessionState
from core.music.player import Unsupported
from core.music.service import ManagedService
from core.utils.logger import logger


class FocusDenied(ManagedError):
    """无法取得/延续 TTS focus（暂停失败、会话已停止等）。

    ``code`` 供接入层区分：`not_playing`/`stopped` 表示无需 focus，
    `pause_unconfirmed` 表示音乐在播但暂停没确认（必须拒绝播报）。
    """

    def __init__(self, msg: str, code: str = "denied") -> None:
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class FocusToken:
    token: str
    session_id: str
    epoch: int
    entry_id: str | None
    position_s: float | None
    provider: str
    created_at: float


@dataclass
class SpeakResult:
    """一个 provider 的独立结局（绝不与其它 provider 合并）。"""

    provider: str
    status: str                 # ok | failed | cancelled | unknown
    started: bool = False
    quality: str | None = None  # strong | weak（仅 ok 时有意义）
    reason: str = ""
    safe_stopped: bool = False

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "status": self.status,
            "started": self.started, "quality": self.quality,
            "reason": self.reason, "safe_stopped": self.safe_stopped,
        }


class TtsSpeaker(Protocol):
    name: str

    async def speak(self, text: str, *, token: FocusToken) -> SpeakResult: ...
    async def cancel(self, token: FocusToken) -> bool: ...


class TtsFocus:
    def __init__(self, service: ManagedService, player, *, clock=time.monotonic,
                 confirm_timeout_s: float = 1.5, poll_s: float = 0.1) -> None:
        self.service = service
        self.store = service.store
        self.player = player
        self.clock = clock
        # 设备暂停不是瞬时生效：必须给一个有界的结算窗口再判「未确认」
        self.confirm_timeout_s = max(0.0, float(confirm_timeout_s))
        self.poll_s = max(0.01, float(poll_s))

    # ---- 取得 focus（先暂停并确认）----

    async def acquire(self, session_id: str, *, provider: str, reason: str = "tts") -> FocusToken:
        state = self._require(session_id)
        if state.intent == Intent.STOPPED or state.phase == Phase.STOPPED:
            raise FocusDenied("session is stopped", code="stopped")
        if state.focus_token:
            # 已在 focus 中（连续播报）：复用现有暂停，只换令牌
            entry_id = state.current.entry_id if state.current else None
            token = self._issue(state, entry_id, provider)
            self._set_focus(state.session_id, token.token)
            return token
        if state.phase != Phase.PLAYING:
            raise FocusDenied("music is not playing; refuse to take focus", code="not_playing")
        await self.player.pause(session_id)
        state_now = await self._wait_state("paused")
        if state_now == "idle":
            # 起播后立刻播报时，设备暂停后可能先回 idle（上下文重建/刚起播）。
            # 这里若按「音乐已结束」放弃焦点，phase 会永远停在 paused 且没人恢复
            # （SLO/真机短播报复现）。暂停命令已下发 ⇒ 视为暂停生效：释放时
            # ensure_resumed 会回读并 restart 当前 entry。
            logger.info(
                "[TtsFocus] pause read back as idle; treating pause as effective"
            )
        elif state_now != "paused":
            raise FocusDenied(f"pause not confirmed before {reason}", code="pause_unconfirmed")
        paused = self._require(session_id)          # pause 会 bump epoch
        entry_id = paused.current.entry_id if paused.current else None
        token = self._issue(paused, entry_id, provider)
        self._set_focus(session_id, token.token)
        return token

    async def _wait_state(self, want: str) -> str:
        """有界结算：设备暂停需要一会儿才在回读里体现。"""

        deadline = self.clock() + self.confirm_timeout_s
        state = await self.player.backend.state()
        while state != want and self.clock() < deadline:
            await asyncio.sleep(self.poll_s)
            state = await self.player.backend.state()
        return state

    # ---- 播报：每个 provider 独立结果 ----

    async def speak(self, token: FocusToken, text: str,
                    speakers: list[TtsSpeaker]) -> list[SpeakResult]:
        results: list[SpeakResult] = []
        for speaker in speakers:
            try:
                result = await speaker.speak(text, token=token)
            except Exception as exc:  # noqa: BLE001 - 单个 provider 失败不牵连其它
                result = SpeakResult(speaker.name, "failed", False, reason=str(exc)[:160])
            if result.status in ("unknown",) or (
                result.status == "ok" and result.quality == "weak"
            ):
                # 弱/未知完成：先安全停止这一段 TTS，再交回调用方决定后续
                try:
                    await speaker.cancel(token)
                    result.safe_stopped = True
                except Exception:  # noqa: BLE001
                    result.safe_stopped = False
            results.append(result)
        return results

    async def cancel(self, token: FocusToken, speakers: list[TtsSpeaker]) -> int:
        cancelled = 0
        for speaker in speakers:
            try:
                if await speaker.cancel(token):
                    cancelled += 1
            except Exception:  # noqa: BLE001
                continue
        return cancelled

    # ---- 释放：只有同一 focus 且可恢复才继续 ----

    async def release(self, token: FocusToken) -> tuple[bool, str]:
        state = self.store.get(token.session_id)
        if state is None:
            return False, "session_gone"
        logger.debug(
            f"[TtsFocus] release start phase={state.phase.value} intent={state.intent.value} "
            f"epoch={state.epoch}/{token.epoch} focus_match={state.focus_token == token.token}"
        )
        # 先给最有信息量的拒绝原因，再做令牌/代次比对
        if state.intent == Intent.STOPPED or state.phase == Phase.STOPPED:
            return False, "stopped"
        if state.epoch != token.epoch:
            return False, "epoch_changed"
        if state.focus_token != token.token:
            return False, "focus_superseded"
        current = state.current
        if token.entry_id and (current is None or current.entry_id != token.entry_id):
            return False, "entry_changed"
        backend_now = await self.player.backend.state()
        if backend_now == "playing":
            # 确认有别的音频在放（外部接管）：relinquish，不抢回
            logger.info(f"[TtsFocus] focus relinquished (state={backend_now})")
            self._set_focus(token.session_id, None)
            return False, "external_takeover"
        # paused / idle / unknown：一律交给 ensure_resumed 回读验证——
        # unknown 常见于 TTS 刚结束、player_context 一时读不到，不能当外部接管。
        self._set_focus(token.session_id, None)
        try:
            outcome = await self.player.ensure_resumed(token.session_id)
        except Unsupported as exc:
            logger.info(f"[TtsFocus] resume unsupported: {exc}")
            return False, "resume_unsupported"
        if outcome == "restarted":
            logger.info("[TtsFocus] media context was lost after TTS; restarted current entry")
        else:
            logger.debug("[TtsFocus] music resumed after focus")
        return True, outcome

    def invalidate(self, session_id: str, *, reason: str = "command") -> None:
        """用户命令/外部事件作废恢复资格（不改变暂停状态）。"""

        self._set_focus(session_id, None)

    # ---- 内部 ----

    def _require(self, session_id: str) -> SessionState:
        state = self.store.get(session_id)
        if state is None:
            raise FocusDenied(f"unknown session {session_id!r}")
        return state

    def _issue(self, state: SessionState, entry_id: str | None, provider: str) -> FocusToken:
        position = state.position.position_s if state.position else None
        return FocusToken(
            token=f"f-{uuid.uuid4().hex[:10]}",
            session_id=state.session_id,
            epoch=state.epoch,
            entry_id=entry_id,
            position_s=position,
            provider=str(provider or "tts"),
            created_at=self.clock(),
        )

    def _set_focus(self, session_id: str, token_id: str | None) -> None:
        state = self.store.get(session_id)
        if state is None:
            return

        def mutate(current: SessionState) -> None:
            current.focus_token = token_id

        self.store.transition(session_id, mutate)
