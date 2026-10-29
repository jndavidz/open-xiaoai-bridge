"""托管播放事件总线（B-05）。

持久化在 `ManagedStore.managed_events`（与状态同一事务写入）；本模块只负责
**在线扇出**：

  - 每个订阅者有界队列，慢消费者不阻塞发布方；溢出后下一次读取返回
    `{"kind": "reset"}`，由订阅端用快照游标重建（缺口 reset）。
  - 回放（含 Last-Event-ID 与缺口判定）直接读 store，保证重启后仍可续。
"""

from __future__ import annotations

import asyncio
from typing import Any


RESET_KIND = "reset"


class Subscription:
    def __init__(self, bus: "EventBus", session_id: str, max_queue: int) -> None:
        self._bus = bus
        self.session_id = session_id
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(1, int(max_queue)))
        self._overflow = False
        self._closed = False

    def offer(self, event: dict) -> None:
        if self._closed:
            return
        if self._overflow:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # 慢消费者：清空积压并标记 reset，绝不让发布方等待
            self._overflow = True
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def get(self, timeout: float = 15.0) -> dict | None:
        """取一条事件；None 表示超时（调用方应发心跳）。溢出后返回 reset。"""

        if self._overflow:
            self._overflow = False
            return {"kind": RESET_KIND, "reason": "slow_consumer"}
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._closed = True


class EventBus:
    def __init__(self, store, *, max_queue: int = 64) -> None:
        self._store = store
        self._max_queue = max(1, int(max_queue))
        self._subs: dict[str, list[Subscription]] = {}

    def subscribe(self, session_id: str) -> Subscription:
        sub = Subscription(self, session_id, self._max_queue)
        self._subs.setdefault(session_id, []).append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        sub.close()
        bucket = self._subs.get(sub.session_id)
        if bucket and sub in bucket:
            bucket.remove(sub)

    def publish(self, session_id: str, event: dict[str, Any]) -> None:
        for sub in list(self._subs.get(session_id, [])):
            sub.offer(event)

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subs.get(session_id, []))

    def replay(self, session_id: str, after_seq: int, limit: int = 200) -> tuple[list[dict], bool]:
        return self._store.events_after(session_id, after_seq, limit)
