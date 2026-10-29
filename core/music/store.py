"""托管播放会话/队列存储（B-04）。

单写者语义：所有意图变化都在 store 的锁内完成，并且**一次提交**把会话状态
（含队列、当前项、位置、恢复资格）写进 SQLite —— 不会出现「内存变了、磁盘
没变」的中间态。重启恢复只读元数据并进入 recovery_pending，绝不自动起播。

表结构是 additive 的（`managed_sessions` 一行一会话，payload 为 JSON）；后续
票只加列/加表，不改既有语义。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from core.music.model import (
    Intent,
    ManagedError,
    Phase,
    PositionSnapshot,
    QueueEntry,
    SessionNotFound,
    SessionState,
    StaleEpoch,
    TrackRef,
)

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "managed" / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_sessions (
    session_id TEXT PRIMARY KEY,
    epoch      INTEGER NOT NULL,
    updated_at REAL    NOT NULL,
    payload    TEXT    NOT NULL
);
"""


class ManagedStore:
    def __init__(self, path: str | Path | None = DEFAULT_DB, *, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        if path is None:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(p), check_same_thread=False)
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # ---- 读 ----

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            return self._load(session_id)

    def sessions(self) -> list[SessionState]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM managed_sessions ORDER BY updated_at"
            ).fetchall()
        return [self._decode(row[0]) for row in rows]

    # ---- 写（每次一个事务）----

    def open_session(
        self,
        *,
        transport: str = "dlna",
        player_backend: str = "mediaplayer",
        lease_id: str = "",
        session_id: str | None = None,
    ) -> SessionState:
        state = SessionState(
            session_id=session_id or f"ms-{uuid.uuid4().hex[:12]}",
            epoch=1,
            phase=Phase.PREPARING,
            intent=Intent.PAUSED,
            transport=str(transport),
            player_backend=str(player_backend),
            lease_id=str(lease_id),
        )
        self._save(state)
        return state

    def append(
        self,
        session_id: str,
        ref: TrackRef,
        *,
        quality: str = "320k",
        epoch: int | None = None,
    ) -> QueueEntry:
        state = self._require(session_id)
        self._check_epoch(state, epoch)
        entry = QueueEntry(entry_id=f"e-{uuid.uuid4().hex[:10]}", ref=ref,
                           quality=str(quality or "320k"))
        state.entries.append(entry)
        self._save(state)
        return entry

    def set_current(self, session_id: str, entry_id: str, *, epoch: int | None = None) -> SessionState:
        state = self._require(session_id)
        self._check_epoch(state, epoch)
        index = next((i for i, e in enumerate(state.entries) if e.entry_id == entry_id), -1)
        if index < 0:
            raise ManagedError(f"unknown queue entry {entry_id!r}")
        state.current_index = index
        state.phase = Phase.PREPARING
        self._save(state)
        return state

    def mark_playing(self, session_id: str, *, epoch: int | None = None,
                     position: PositionSnapshot | None = None) -> SessionState:
        state = self._require(session_id)
        self._check_epoch(state, epoch)
        state.phase = Phase.PLAYING
        state.intent = Intent.PLAYING
        if position is not None:
            self._check_snapshot(state, position)
            state.position = position
        self._save(state)
        return state

    def pause(self, session_id: str, *, epoch: int | None = None) -> SessionState:
        state = self._require(session_id)
        self._check_epoch(state, epoch)
        state.epoch += 1          # 暂停即作废进行中的命令
        state.phase = Phase.PAUSED
        state.intent = Intent.PAUSED
        state.auto_advance = False
        self._save(state)
        return state

    def resume(self, session_id: str, *, epoch: int | None = None) -> SessionState:
        state = self._require(session_id)
        self._check_epoch(state, epoch)
        state.epoch += 1
        state.phase = Phase.PLAYING
        state.intent = Intent.PLAYING
        self._save(state)
        return state

    def stop(self, session_id: str, *, epoch: int | None = None, reason: str = "user") -> SessionState:
        """停止：**先**作废恢复资格与自动推进，再落盘（同一事务）。"""

        state = self._require(session_id)
        self._check_epoch(state, epoch)
        state.epoch += 1
        state.phase = Phase.STOPPED
        state.intent = Intent.STOPPED
        state.auto_advance = False
        state.recovery_token = None
        state.stop_reason = str(reason or "user")
        self._save(state)
        return state

    def grant_recovery(self, session_id: str, token: str, *, epoch: int | None = None) -> SessionState:
        """授予恢复资格：只在播放/暂停态有效，stop 之后永远拒绝。"""

        state = self._require(session_id)
        self._check_epoch(state, epoch)
        if state.intent == Intent.STOPPED:
            raise ManagedError("stopped session cannot grant recovery")
        state.recovery_token = str(token)
        self._save(state)
        return state

    def invalidate_recovery(self, session_id: str) -> SessionState:
        state = self._require(session_id)
        state.recovery_token = None
        self._save(state)
        return state

    def set_position(self, session_id: str, snapshot: PositionSnapshot, *,
                     epoch: int | None = None) -> SessionState:
        state = self._require(session_id)
        self._check_epoch(state, epoch)
        self._check_snapshot(state, snapshot)
        state.position = snapshot
        self._save(state)
        return state

    def bump_epoch(self, session_id: str) -> int:
        state = self._require(session_id)
        state.epoch += 1
        self._save(state)
        return state.epoch

    # ---- 恢复 ----

    def restore(self) -> list[SessionState]:
        """重启恢复：只还原元数据，全部进入 recovery_pending；不授予恢复资格。"""

        restored: list[SessionState] = []
        for state in self.sessions():
            state.phase = Phase.RECOVERY_PENDING
            state.intent = Intent.PAUSED
            state.recovery_token = None
            state.auto_advance = False
            self._save(state)
            restored.append(state)
        return restored

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 内部 ----

    def _require(self, session_id: str) -> SessionState:
        state = self._load(session_id)
        if state is None:
            raise SessionNotFound(session_id)
        return state

    def _load(self, session_id: str) -> SessionState | None:
        row = self._conn.execute(
            "SELECT payload FROM managed_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return self._decode(row[0]) if row else None

    @staticmethod
    def _check_epoch(state: SessionState, epoch: int | None) -> None:
        if epoch is not None and int(epoch) != state.epoch:
            raise StaleEpoch(f"session {state.session_id}: epoch {epoch} != {state.epoch}")

    @staticmethod
    def _check_snapshot(state: SessionState, snapshot: PositionSnapshot) -> None:
        if snapshot.transport != state.transport:
            raise ManagedError(
                f"position transport {snapshot.transport!r} != session {state.transport!r}")
        if snapshot.player_backend != state.player_backend:
            raise ManagedError(
                f"position backend {snapshot.player_backend!r} != session "
                f"{state.player_backend!r}")

    def _save(self, state: SessionState) -> None:
        state.updated_at = self._clock()
        payload = json.dumps(self._encode(state), ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO managed_sessions(session_id, epoch, updated_at, payload) "
                "VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "epoch=excluded.epoch, updated_at=excluded.updated_at, payload=excluded.payload",
                (state.session_id, state.epoch, state.updated_at, payload),
            )

    @staticmethod
    def _encode(state: SessionState) -> dict:
        return {
            "session_id": state.session_id,
            "epoch": state.epoch,
            "phase": state.phase.value,
            "intent": state.intent.value,
            "transport": state.transport,
            "player_backend": state.player_backend,
            "lease_id": state.lease_id,
            "entries": [
                {"entry_id": e.entry_id, "quality": e.quality,
                 "source": e.ref.source, "id": e.ref.id,
                 "extra": e.ref.extra_dict}
                for e in state.entries
            ],
            "current_index": state.current_index,
            "position": state.position.as_dict() if state.position else None,
            "recovery_token": state.recovery_token,
            "auto_advance": state.auto_advance,
            "stop_reason": state.stop_reason,
            "updated_at": state.updated_at,
        }

    @staticmethod
    def _decode(payload: str) -> SessionState:
        data = json.loads(payload)
        return SessionState(
            session_id=data["session_id"],
            epoch=int(data["epoch"]),
            phase=Phase(data["phase"]),
            intent=Intent(data["intent"]),
            transport=data["transport"],
            player_backend=data["player_backend"],
            lease_id=data.get("lease_id", ""),
            entries=[
                QueueEntry(entry_id=e["entry_id"],
                           ref=TrackRef.make(e["source"], e["id"], e.get("extra")),
                           quality=e.get("quality", "320k"))
                for e in data.get("entries") or []
            ],
            current_index=int(data.get("current_index", -1)),
            position=PositionSnapshot.from_dict(data.get("position")),
            recovery_token=data.get("recovery_token"),
            auto_advance=bool(data.get("auto_advance", False)),
            stop_reason=data.get("stop_reason", ""),
            updated_at=float(data.get("updated_at", 0.0)),
        )
