"""托管播放的会话/队列/存储与单设备执行器（B-04）。

默认不接管生产流量：本包只提供模型与存储原语，接线由后续票（B-05）完成。
"""

from core.music.auth import MusicAuth, MusicAuthError, MusicGrant
from core.music.backends import DlnaBackend, RpcBackend
from core.music.player import (
    DLNA,
    RPC,
    BackendProfile,
    Decision,
    ManagedQueuePlayer,
    Unsupported,
)
from core.music.events import EventBus, Subscription
from core.music.executor import CommandTicket, ManagedExecutor
from core.music.service import (
    COMMANDS,
    ConflictError,
    ManagedService,
    Receipt,
    TargetMismatch,
)
from core.music.model import (
    CommandState,
    Intent,
    ManagedError,
    Phase,
    PositionSnapshot,
    QueueEntry,
    QueueFull,
    SessionNotFound,
    SessionState,
    StaleEpoch,
    TrackRef,
)
from core.music.store import ManagedStore

__all__ = [
    "COMMANDS",
    "DLNA",
    "RPC",
    "BackendProfile",
    "Decision",
    "DlnaBackend",
    "ManagedQueuePlayer",
    "RpcBackend",
    "Unsupported",
    "CommandState",
    "CommandTicket",
    "ConflictError",
    "EventBus",
    "Intent",
    "MusicAuth",
    "MusicAuthError",
    "MusicGrant",
    "ManagedService",
    "Receipt",
    "Subscription",
    "TargetMismatch",
    "ManagedError",
    "ManagedExecutor",
    "ManagedStore",
    "Phase",
    "PositionSnapshot",
    "QueueEntry",
    "QueueFull",
    "SessionNotFound",
    "SessionState",
    "StaleEpoch",
    "TrackRef",
]
