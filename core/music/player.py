"""托管播放的 EOF 判定与队列推进（B-06）。

设计要点（主 spec "bridge commands, EOF and TTS"）：

  - **不依赖自然 STOPPED**：单 URI 的 DLNA 在 loop=0/1 下都会重播（真机已证），
    所以推进判据是「可信时长 + 新鲜位置 + 当前意图」，到点后**主动 Stop 并确认**，
    再换下一条；末条 Stop 后结束。
  - **能力矩阵按 backend 分开**：DLNA 与 mediaplayer RPC 的 position/seek/resume/stop
    各自登记，未验证的能力一律 `Unsupported`，绝不把一种 backend 的样本复制给另一种。
  - **暂停 / TTS / 缓冲停滞**：暂停冻结计时；位置陈旧且未到点 ⇒ stalled；时长未知 ⇒
    unknown；这些都不推进。
  - **回绕补偿只做一次**，且仅限显式声明 supported 的 backend。
  - **外部接管即 relinquish**：不再 Stop、不再推进，也不抢回。

本模块只做判定与编排；设备 I/O 由注入的 backend 完成，测试用 fake clock/backend。
"""

from __future__ import annotations

import dataclasses

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable

from core.music.model import Intent, ManagedError, Phase, QueueEntry, SessionState
from core.music.store import ManagedStore


class Unsupported(ManagedError):
    """backend/状态不支持的托管操作（如实上报，不假装成功）。"""


class Decision(str, Enum):
    WAIT = "wait"
    ADVANCE = "advance"
    FINISH = "finish"
    STALLED = "stalled"
    UNKNOWN = "unknown"
    RELINQUISH = "relinquish"


@dataclass(frozen=True)
class BackendProfile:
    """一个 backend 的能力矩阵（未验证 = False）。"""

    name: str
    position: bool = False
    seek: bool = False
    resume: bool = False
    stop: bool = True
    wraparound_compensation: bool = False


# 事实层：DLNA 投送只能 Stop/换 URI（SOAP seek/resume 未验证）；RPC 走设备播放控制 CLI，
# 能 play/pause/stop，但没有已验证的位置回读。两者都不声称支持 seek。
DLNA = BackendProfile(name="dlna", position=False, seek=False, resume=False, stop=True)
RPC = BackendProfile(name="rpc-mediaplayer", position=False, seek=False, resume=True,
                     stop=True, wraparound_compensation=False)


class EndOfTrackDetector:
    """单条 entry 的完成判定（纯逻辑，注入时钟）。"""

    def __init__(
        self,
        *,
        duration_s: float,
        backend: BackendProfile,
        guard_s: float = 1.5,
        stall_s: float = 10.0,
        fresh_position_s: float = 5.0,
        wrap_ratio: float = 0.5,
    ) -> None:
        self.duration_s = max(0.0, float(duration_s or 0.0))
        self.backend = backend
        self.guard_s = max(0.0, float(guard_s))
        self.stall_s = max(0.0, float(stall_s))
        self.fresh_position_s = max(0.0, float(fresh_position_s))
        self.wrap_ratio = min(0.9, max(0.1, float(wrap_ratio)))
        self.started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0
        self._last_position: float | None = None
        self._last_position_at: float | None = None
        self._max_position = 0.0
        self._wraps = 0
        self.advanced = False
        self.relinquished = False

    # ---- 观测 ----

    def on_play(self, now: float) -> None:
        self.started_at = now

    def on_pause(self, now: float) -> None:
        if self._paused_at is None:
            self._paused_at = now

    def on_resume(self, now: float) -> None:
        if self._paused_at is not None:
            self._paused_total += max(0.0, now - self._paused_at)
            self._paused_at = None

    def on_position(self, now: float, position_s: float) -> None:
        position_s = max(0.0, float(position_s))
        self._last_position = position_s
        self._last_position_at = now
        self._max_position = max(self._max_position, position_s)

    def on_external_takeover(self) -> None:
        self.relinquished = True

    def mark_advanced(self) -> None:
        self.advanced = True

    # ---- 判定 ----

    def elapsed(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        paused = self._paused_total
        if self._paused_at is not None:
            paused += max(0.0, now - self._paused_at)
        return max(0.0, (now - self.started_at) - paused)

    def evaluate(self, now: float) -> Decision:
        if self.relinquished:
            return Decision.RELINQUISH
        if self.advanced:
            return Decision.WAIT
        if self.duration_s <= 0:
            return Decision.UNKNOWN   # 未知时长：宁可停住，不瞎猜
        if self._paused_at is not None:
            return Decision.WAIT      # 暂停冻结

        threshold = max(0.0, self.duration_s - self.guard_s)
        if self.backend.position and self._last_position is not None:
            age = now - (self._last_position_at or 0.0)
            if age <= self.fresh_position_s:
                if self._last_position >= threshold:
                    return Decision.ADVANCE
                if (
                    self.backend.wraparound_compensation
                    and self._last_position < self._max_position * (1.0 - self.wrap_ratio)
                ):
                    # 位置回绕：一次补偿推进（同一首只允许一次）
                    self._wraps += 1
                    if self._wraps <= 1:
                        return Decision.ADVANCE
                return Decision.WAIT
            if age > self.stall_s and self.elapsed(now) < threshold:
                return Decision.STALLED

        if self.elapsed(now) >= threshold:
            return Decision.ADVANCE
        return Decision.WAIT


class PlaybackBackend:
    """设备后端接口（测试注入 fake；生产由 B-06 的 DLNA/RPC 适配实现）。"""

    profile: BackendProfile

    async def play(self, uri: str) -> bool: ...
    async def stop(self) -> bool: ...
    async def pause(self) -> bool: ...
    async def resume(self) -> bool: ...
    async def state(self) -> str: ...          # playing / paused / idle / unknown
    async def position(self) -> float | None: ...


class ManagedQueuePlayer:
    """只准备当前 entry；到点主动 Stop 并确认，再推进一次。"""

    def __init__(
        self,
        store: ManagedStore,
        backend: PlaybackBackend,
        resolve: Callable[[QueueEntry], Awaitable[str]],
        *,
        clock: Callable[[], float] = time.monotonic,
        stop_confirm_attempts: int = 2,
    ) -> None:
        self.store = store
        self.backend = backend
        self.resolve = resolve
        self.clock = clock
        self.stop_confirm_attempts = max(1, int(stop_confirm_attempts))
        self._detector: EndOfTrackDetector | None = None
        self._entry_id: str | None = None
        self._prepared_uri: str | None = None
        self._prepared_entry: str | None = None
        self._prepared_duration: float = 0.0

    # ---- 起播（只准备当前 entry）----

    async def start(self, session_id: str, *, epoch: int | None = None) -> bool:
        state = self._require(session_id)
        entry = state.current
        if entry is None:
            raise ManagedError("queue has no current entry")
        uri = None
        resolved_duration = 0.0
        if self._prepared_entry == entry.entry_id:
            uri = self._prepared_uri        # 只准备当前项：同一条复用已解析 URI
            resolved_duration = getattr(self, "_prepared_duration", 0.0)
        if not uri:
            resolved = await self.resolve(entry)
            # 解析器可顺带返回验证过的时长（Songloft 准备链）；HTTP/App 入队没有
            # duration_s 时靠它，否则 EOF 判定永远 unknown、播完不推进。
            if isinstance(resolved, tuple):
                uri, resolved_duration = resolved
                resolved_duration = float(resolved_duration or 0.0)
            else:
                uri = resolved
            self._prepared_entry, self._prepared_uri = entry.entry_id, uri
            self._prepared_duration = resolved_duration
        if not await self.backend.play(uri):
            raise ManagedError("backend rejected playback")
        duration = entry.duration_s or resolved_duration
        if duration and not entry.duration_s:
            self.store.transition(
                session_id,
                lambda state: _set_entry_duration(state, entry.entry_id, duration),
            )
            entry = self._require(session_id).current or entry
        detector = EndOfTrackDetector(duration_s=duration, backend=self.backend.profile)
        now = self.clock()
        detector.on_play(now)
        self._detector, self._entry_id = detector, entry.entry_id
        self.store.transition(
            session_id, _mark_playing,
            events=[{"kind": "playing", "entry_id": entry.entry_id}],
            epoch=epoch,
        )
        return True

    def skip_failed(self, session_id: str, reason: str) -> bool:
        """起播失败（解析/准备/设备拒绝）后跳过当前项，返回是否还有下一项。

        失败必须可见：写 `advance_failed` 事件；末项失败即置 stopped，
        **绝不停留在 preparing**（运行期会一直 WAIT，用户看到「没有播放」却无原因）。
        """

        state = self._require(session_id)
        if state.phase is Phase.STOPPED or state.intent is Intent.STOPPED:
            # 用户已 stop / 会话已结束：解析失败不能把它重新拉回 preparing
            return False
        failed = state.current.entry_id if state.current else None
        has_next = state.current_index + 1 < len(state.entries)
        self._detector = None
        self._prepared_uri = None
        self._prepared_entry = None
        self._prepared_duration = 0.0
        self.store.transition(
            session_id,
            _skip_to_next if has_next else _mark_advance_failed,
            events=[{"kind": "advance_failed", "entry_id": failed,
                     "detail": str(reason)[:160]}],
        )
        return has_next

    # ---- 推进 / 结束 ----

    async def tick(self, session_id: str, *, epoch: int | None = None) -> Decision:
        state = self._require(session_id)
        detector = self._detector
        if detector is None or self._entry_id is None:
            return Decision.WAIT
        decision = detector.evaluate(self.clock())
        if decision is not Decision.ADVANCE:
            return decision
        if state.intent is not Intent.PLAYING:
            return Decision.WAIT
        # 入场时快照：Device Stop 是 await 点，期间另一个 tick 可能已推进并
        # 把 self._entry_id 换成新 entry——拿它做比较会恒等，必须用快照。
        tracked = self._entry_id
        # 主动 Stop 并确认（不等自然 STOPPED，也不用设备 Next 冒充队列推进）
        if not await self._stop_and_confirm():
            return Decision.UNKNOWN
        state = self._require(session_id)
        current = state.current.entry_id if state.current else None
        if self._entry_id != tracked or current != tracked:
            # 只有当检测器仍对应被跟踪的那条时才清；否则那是别人的新检测器。
            if self._entry_id == tracked:
                self._detector = None
            return Decision.WAIT
        detector.mark_advanced()
        index = state.current_index
        if index + 1 >= len(state.entries):
            self.store.transition(
                session_id, _mark_finished,
                events=[{"kind": "queue_finished", "entry_id": self._entry_id}],
                epoch=epoch,
            )
            self._detector = None
            return Decision.FINISH
        next_entry = state.entries[index + 1]
        self.store.transition(
            session_id, _move_to(index + 1),
            events=[{"kind": "advance", "from": self._entry_id,
                     "to": next_entry.entry_id}],
            epoch=epoch,
        )
        self._detector = None
        self._prepared_uri = None
        self._prepared_entry = None
        self._prepared_duration = 0.0
        return Decision.ADVANCE

    async def _stop_and_confirm(self) -> bool:
        if not self.backend.profile.stop:
            raise Unsupported(f"backend {self.backend.profile.name} has no verified stop")
        for _ in range(self.stop_confirm_attempts):
            if await self.backend.stop() and await self.backend.state() == "idle":
                return True
        return False

    # ---- 暂停 / seek / 外部接管 ----

    async def ensure_resumed(self, session_id: str, *, verify_timeout_s: float = 0.8) -> str:
        """恢复并**回读验证**：设备 play 成功 ≠ 真的在放。

        TTS/唤醒可能已把媒体上下文替换掉，此时 play 是「假成功」（code:0 但无音频）。
        验证窗口内没回到 playing ⇒ 重新解析并重起当前 entry。
        """

        state = await self.backend.state()
        if state == "paused":
            try:
                await self.resume_backend()
            except Unsupported:
                pass
        after = await self._wait_backend_state("playing", verify_timeout_s)
        if after == "playing":
            self.store.transition(session_id, _mark_resumed, events=[{"kind": "resumed"}])
            return "resumed"
        await self.restart(session_id)
        return "restarted"

    async def _wait_backend_state(self, want: str, timeout_s: float) -> str:
        deadline = self.clock() + max(0.0, float(timeout_s))
        state = await self.backend.state()
        while state != want and self.clock() < deadline:
            await asyncio.sleep(0.1)
            state = await self.backend.state()
        return state

    async def restart(self, session_id: str, *, epoch: int | None = None) -> bool:
        """重新起播当前 entry（设备上下文已被 TTS/中断顶掉时的显式恢复）。

        强制重新解析 URI：旧直链可能已过期；这不是「偷偷 seek」——由用户的
        显式恢复命令触发，且从当前 entry 头开始。
        """

        self._prepared_uri = None
        self._prepared_entry = None
        return await self.start(session_id, epoch=epoch)

    async def pause_backend(self) -> None:
        """只驱动设备暂停 + 冻结探测器（状态/回执由命令层负责）。"""

        if self._detector is None:
            raise Unsupported("nothing is playing")
        if not await self.backend.pause():
            raise ManagedError("backend rejected pause")
        self._detector.on_pause(self.clock())

    async def resume_backend(self) -> None:
        """只驱动设备恢复 + 解冻探测器（未声明 resume 的 backend 明拒）。"""

        if self._detector is None:
            raise Unsupported("nothing is playing")
        if not self.backend.profile.resume:
            raise Unsupported(f"backend {self.backend.profile.name} has no verified resume")
        if not await self.backend.resume():
            raise ManagedError("backend rejected resume")
        self._detector.on_resume(self.clock())

    async def pause(self, session_id: str) -> None:
        await self.pause_backend()
        self.store.transition(session_id, _mark_paused, events=[{"kind": "paused"}])

    async def resume(self, session_id: str) -> None:
        await self.resume_backend()
        self.store.transition(session_id, _mark_resumed, events=[{"kind": "resumed"}])

    async def seek(self, session_id: str, position_s: float) -> None:
        """停止后迟到 seek 不得偷偷 Play；未验证能力的 backend 一律明拒。"""

        state = self._require(session_id)
        if not self.backend.profile.seek:
            raise Unsupported(f"backend {self.backend.profile.name} has no verified seek")
        if state.phase not in (Phase.PLAYING, Phase.PAUSED):
            raise Unsupported("seek requires an active playback")
        raise Unsupported("seek is not implemented for the managed adapter yet")

    def update_position(self, position_s: float) -> None:
        """喂入一次位置观测（仅 position 能力已声明的 backend 有意义）。"""

        if self._detector is not None:
            self._detector.on_position(self.clock(), position_s)

    def on_external_takeover(self) -> None:
        if self._detector is not None:
            self._detector.on_external_takeover()

    # ---- 内部 ----

    def _require(self, session_id: str) -> SessionState:
        state = self.store.get(session_id)
        if state is None:
            raise ManagedError(f"unknown session {session_id!r}")
        return state


def _skip_to_next(state: SessionState) -> None:
    state.epoch += 1
    state.current_index = min(state.current_index + 1, len(state.entries) - 1)
    state.phase = Phase.PREPARING
    state.intent = Intent.PLAYING
    state.auto_advance = True


def _mark_advance_failed(state: SessionState) -> None:
    state.epoch += 1
    state.phase = Phase.STOPPED
    state.intent = Intent.STOPPED
    state.auto_advance = False
    state.stop_reason = "advance_failed"


def _set_entry_duration(state: SessionState, entry_id: str, duration_s: float) -> None:
    """把解析阶段得到的可信时长写回队列项（只补缺，不改已有值；QueueEntry 不可变）。"""

    for index, entry in enumerate(state.entries):
        if entry.entry_id == entry_id and not entry.duration_s:
            state.entries[index] = dataclasses.replace(
                entry, duration_s=max(0.0, float(duration_s or 0.0)))
            return


def _mark_playing(state: SessionState) -> None:
    state.epoch += 1
    state.phase = Phase.PLAYING
    state.intent = Intent.PLAYING


def _mark_paused(state: SessionState) -> None:
    state.epoch += 1
    state.phase = Phase.PAUSED
    state.intent = Intent.PAUSED
    state.auto_advance = False


def _mark_resumed(state: SessionState) -> None:
    state.epoch += 1
    state.phase = Phase.PLAYING
    state.intent = Intent.PLAYING


def _mark_finished(state: SessionState) -> None:
    state.phase = Phase.STOPPED
    state.intent = Intent.STOPPED
    state.stop_reason = "end_of_queue"
    state.auto_advance = False
    state.recovery_token = None


def _move_to(index: int):
    def mutate(state: SessionState) -> None:
        state.current_index = index
        state.phase = Phase.PREPARING
    return mutate
