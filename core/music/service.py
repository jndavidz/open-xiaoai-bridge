"""托管播放命令门面（B-05）。

所有入口（Agent 工具 / HTTP / KWS / 原生 ASR / HA）都只经过 `ManagedService.command`：
  - 幂等回执：同 principal + 同 key + 同 payload 返回原回执（replayed=True）；
    同 key 不同 payload ⇒ ConflictError（409）。
  - 代次闸门：带 expected_epoch 的命令必须匹配当前代次，否则 StaleEpoch。
  - 状态与关键事件**同一 SQLite 事务**写入（store.transition）。
  - 命令状态分离：state-only 的 pause/resume/stop 是 applied；需要设备执行的
    play/seek/next/prev 先回 accepted（设备接线在 B-06）。
  - target 必须与会话一致（music-control scope 的 target 维度）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from core.music.budget import RetryBudget
from core.music.events import EventBus
import sqlite3

from core.music.model import (
    CommandState,
    Intent,
    ManagedError,
    Phase,
    SessionNotFound,
    SessionState,
    StaleEpoch,
)
from core.music.store import ManagedStore


class ConflictError(ManagedError):
    """同一幂等键被不同 payload 复用（409）。"""


class TargetMismatch(ManagedError):
    """命令 target 与会话 target 不一致（403）。"""


COMMANDS: frozenset[str] = frozenset(
    {"play", "pause", "resume", "stop", "next", "prev", "seek"}
)
# 只改状态、不需设备执行 ⇒ applied；其余先 accepted，等 B-06 的设备层回执
_STATE_ONLY = frozenset({"pause", "resume", "stop"})


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    session_id: str
    kind: str
    state: str
    epoch: int
    replayed: bool = False
    budget_remaining_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "state": self.state,
            "epoch": self.epoch,
            "replayed": self.replayed,
            "budget_remaining_ms": self.budget_remaining_ms,
        }


def _digest(kind: str, payload: dict, target: str) -> str:
    blob = json.dumps({"kind": kind, "payload": payload or {}, "target": target},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ManagedService:
    def __init__(
        self,
        *,
        store: ManagedStore | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.store = store or ManagedStore()
        self.bus = bus or EventBus(self.store)

    # ---- 会话 ----

    def open_session(
        self,
        *,
        target: str,
        transport: str = "dlna",
        player_backend: str = "mediaplayer",
        lease_id: str = "",
        principal: str = "",
    ) -> dict:
        state, stored = self.store.create_session(
            transport=transport, player_backend=player_backend,
            lease_id=lease_id, target=target,
            events=[{"kind": "session_opened", "epoch": None, "principal": principal,
                     "target": target}],
        )
        for event in stored:
            self.bus.publish(state.session_id, event)
        return self.snapshot(state.session_id)

    def snapshot(self, session_id: str) -> dict:
        state = self.store.get(session_id)
        if state is None:
            raise SessionNotFound(session_id)
        snapshot = state.as_dict()
        snapshot["last_seq"] = self.store.last_event_seq(session_id)
        return snapshot

    # ---- 命令 ----

    def command(
        self,
        session_id: str,
        *,
        kind: str,
        payload: dict | None = None,
        principal: str,
        idem_key: str,
        expected_epoch: int | None = None,
        target: str | None = None,
        budget: RetryBudget | None = None,
    ) -> Receipt:
        if budget is not None:
            budget.ensure()   # 预算耗尽：交回最外层，不在这里重试
        remaining = budget.remaining_ms if budget is not None else None
        kind = str(kind or "").strip().lower()
        if kind not in COMMANDS:
            raise ManagedError(f"unknown command {kind!r}")
        if not idem_key:
            raise ManagedError("idempotency key is required")
        payload = dict(payload or {})

        existing = self.store.receipt_get(principal, idem_key)
        # 回执键同时绑定会话：同一主体/键在另一个会话上必须重新执行或冲突，
        # 绝不能把 A 会话的回执当成 B 会话的结果返回。
        digest = _digest(f"{session_id}:{kind}", payload, str(target or ""))
        if existing is not None:
            if existing["payload_digest"] != digest:
                raise ConflictError(
                    f"idempotency key {idem_key!r} already used with a different payload")
            return Receipt(existing["receipt_id"], existing["session_id"], existing["kind"],
                           existing["state"], existing["epoch"], replayed=True,
                           budget_remaining_ms=remaining)

        state = self.store.get(session_id)
        if state is None:
            raise SessionNotFound(session_id)
        if kind == "seek":
            # 设备适配（B-06）落地前如实拒绝，不假装接受一个不会执行的能力
            raise ManagedError("seek is not supported by the managed adapter yet")
        if target is not None and state.target and str(target) != state.target:
            raise TargetMismatch(
                f"target {target!r} does not match session target {state.target!r}")

        receipt_id = f"r-{uuid.uuid4().hex[:12]}"
        command_state = (
            CommandState.APPLIED.value if kind in _STATE_ONLY else CommandState.ACCEPTED.value
        )

        def mutate(current: SessionState) -> None:
            _apply(current, kind, payload)

        def receipt_factory(current: SessionState) -> dict:
            return {
                "principal": principal, "idem_key": idem_key, "payload_digest": digest,
                "receipt_id": receipt_id, "session_id": session_id, "kind": kind,
                "state": command_state, "epoch": current.epoch,
            }

        try:
            current, stored = self.store.transition(
                session_id, mutate,
                events=[{"kind": "command", "command": kind, "state": command_state,
                         "epoch": None, "receipt_id": receipt_id, "principal": principal}],
                receipt_factory=receipt_factory,
                epoch=expected_epoch,
            )
        except sqlite3.IntegrityError:
            # 并发同键：回执主键冲突。按「先到者胜」处理，绝不 500。
            winner = self.store.receipt_get(principal, idem_key)
            if winner is not None and winner["payload_digest"] == digest:
                return Receipt(winner["receipt_id"], winner["session_id"], winner["kind"],
                               winner["state"], winner["epoch"], replayed=True,
                               budget_remaining_ms=remaining)
            raise ConflictError(
                f"idempotency key {idem_key!r} already used with a different payload") from None
        for event in stored:
            self.bus.publish(session_id, event)
        return Receipt(receipt_id, session_id, kind, command_state, current.epoch,
                       budget_remaining_ms=remaining)


    # ---- 设备回执：accepted → observed ----

    def observe(self, receipt_id: str, *, state: str = CommandState.OBSERVED.value,
                detail: str = "") -> bool:
        """设备侧确认后推进回执状态（accepted→observed / failed / unknown）。

        只改回执与事件，不动会话代次；未知回执返回 False（调用方按内部错误处理）。
        """

        row = self.store.receipt_by_id(receipt_id)
        if row is None:
            return False
        if not self.store.update_receipt_state(receipt_id, state):
            return False
        session = self.store.get(row["session_id"])
        if session is not None:
            stored = self.store.save_with_events(
                session,
                [{"kind": "command_observed", "receipt_id": receipt_id,
                  "command": row["kind"], "state": state, "detail": detail}],
            )
            for event in stored:
                self.bus.publish(row["session_id"], event)
        return True


def _apply(state: SessionState, kind: str, payload: dict) -> None:
    """单点状态迁移（命令语义）。设备执行不在这里。"""

    if kind == "pause":
        state.epoch += 1
        state.phase = Phase.PAUSED
        state.intent = Intent.PAUSED
        state.auto_advance = False
    elif kind == "resume":
        state.epoch += 1
        state.phase = Phase.PLAYING
        state.intent = Intent.PLAYING
    elif kind == "stop":
        state.epoch += 1
        state.phase = Phase.STOPPED
        state.intent = Intent.STOPPED
        state.auto_advance = False
        state.recovery_token = None
        state.focus_token = None
        state.stop_reason = str(payload.get("reason") or "user")
    elif kind in ("next", "prev"):
        if not state.entries:
            raise ManagedError("queue is empty")
        delta = 1 if kind == "next" else -1
        index = state.current_index + delta
        if index < 0 or index >= len(state.entries):
            raise ManagedError(f"no {kind} entry")
        state.epoch += 1
        state.current_index = index
        state.phase = Phase.PREPARING
    elif kind == "play":
        entry_id = str(payload.get("entry_id") or "")
        if entry_id:
            index = next((i for i, e in enumerate(state.entries) if e.entry_id == entry_id), -1)
            if index < 0:
                raise ManagedError(f"unknown queue entry {entry_id!r}")
            state.current_index = index
        elif state.current_index < 0 and state.entries:
            state.current_index = 0
        state.epoch += 1
        state.phase = Phase.PREPARING
    # seek 在设备适配（B-06）落地前由 command() 直接拒绝，不进迁移。
