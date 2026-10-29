"""托管播放的数据模型（B-04）。

只放不可变的值对象与枚举：身份（TrackRef）、队列项（QueueEntry）、位置快照、
会话状态与会话阶段/意图。存储与执行器分别在 store.py / executor.py。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum


class ManagedError(Exception):
    """托管播放的领域错误基类。"""


class SessionNotFound(ManagedError):
    pass


class StaleEpoch(ManagedError):
    """命令/事件来自旧代次：必须拒绝，不能回写状态。"""


class QueueFull(ManagedError):
    """执行器等待队列已满（有界容量，调用方按 503 处理）。"""


class Intent(str, Enum):
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


class Phase(str, Enum):
    PREPARING = "preparing"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    RECOVERY_PENDING = "recovery_pending"


class CommandState(str, Enum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    OBSERVED = "observed"
    FAILED = "failed"
    UNKNOWN = "unknown"


def _norm_extra(extra: dict | None) -> tuple[tuple[str, str], ...]:
    if not extra:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in extra.items()))


@dataclass(frozen=True)
class TrackRef:
    """稳定曲目身份：source + id + 影响身份的 extra（如 migu copyrightId）。"""

    source: str
    id: str
    extra: tuple[tuple[str, str], ...] = ()

    @classmethod
    def make(cls, source: str, track_id: str, extra: dict | None = None) -> "TrackRef":
        source = str(source or "").strip()
        track_id = str(track_id or "").strip()
        if not source or not track_id:
            raise ManagedError("track_ref requires source and id")
        return cls(source=source, id=track_id, extra=_norm_extra(extra))

    @property
    def extra_dict(self) -> dict[str, str]:
        return dict(self.extra)

    def canonical(self) -> str:
        return json.dumps(
            {"source": self.source, "id": self.id, "extra": self.extra_dict},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class QueueEntry:
    """队列里的一次出现：同一首可以有多条，各自有独立 entry_id。"""

    entry_id: str
    ref: TrackRef
    quality: str = "320k"
    duration_s: float = 0.0     # 可信时长（0 = 未知，EOF 只能进 unknown）


@dataclass(frozen=True)
class PositionSnapshot:
    """位置快照：必须带 backend/transport、采样时刻、操作和新鲜度。

    transport（dlna / rpc）与 player_backend（mediaplayer / 本地播放器等）是
    **两个维度**，不得互相顶替——DLNA 投送时的位置来自投送方，RPC 的位置来自
    设备播放器。
    """

    position_s: float
    sampled_at: float
    operation: str
    fresh_for_s: float
    transport: str
    player_backend: str

    def is_fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return 0.0 <= (now - self.sampled_at) <= max(0.0, self.fresh_for_s)

    def as_dict(self) -> dict:
        return {
            "position_s": round(float(self.position_s), 3),
            "sampled_at": float(self.sampled_at),
            "operation": self.operation,
            "fresh_for_s": float(self.fresh_for_s),
            "transport": self.transport,
            "player_backend": self.player_backend,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "PositionSnapshot | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                position_s=float(data["position_s"]),
                sampled_at=float(data["sampled_at"]),
                operation=str(data["operation"]),
                fresh_for_s=float(data["fresh_for_s"]),
                transport=str(data["transport"]),
                player_backend=str(data["player_backend"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class SessionState:
    session_id: str
    epoch: int
    phase: Phase
    intent: Intent
    transport: str
    player_backend: str
    lease_id: str = ""
    target: str = ""            # 设备目标（music-control scope 的 target 维度）
    entries: list[QueueEntry] = field(default_factory=list)
    current_index: int = -1
    position: PositionSnapshot | None = None
    recovery_token: str | None = None
    auto_advance: bool = False
    stop_reason: str = ""
    updated_at: float = 0.0

    @property
    def current(self) -> QueueEntry | None:
        if 0 <= self.current_index < len(self.entries):
            return self.entries[self.current_index]
        return None

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "epoch": self.epoch,
            "phase": self.phase.value,
            "intent": self.intent.value,
            "transport": self.transport,
            "player_backend": self.player_backend,
            "lease_id": self.lease_id,
            "target": self.target,
            "entries": [
                {"entry_id": e.entry_id, "ref": json.loads(e.ref.canonical()),
                 "quality": e.quality}
                for e in self.entries
            ],
            "current_index": self.current_index,
            "current_entry_id": self.current.entry_id if self.current else None,
            "position": self.position.as_dict() if self.position else None,
            "recovery_token": self.recovery_token,
            "auto_advance": self.auto_advance,
            "stop_reason": self.stop_reason,
            "updated_at": self.updated_at,
        }
