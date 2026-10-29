"""单设备执行器（B-04）：有界队列 + 一个 worker。

纪律：
  - 设备 I/O 只在这里排队执行；`stop` 只改 store 的代次，不排进队列，因此接单
    不会被下载/转码/RPC 拖住。
  - 出队前和执行完成后各校验一次代次：旧代次命令直接丢弃（UNKNOWN），执行中途
    失效的结果也按 UNKNOWN 处理，并做**一次**补偿 Stop —— 绝不盲重发。
  - 队列有界；满时抛 QueueFull，由上层转成明确的忙/容量错误。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.music.model import CommandState, QueueFull

Ticketer = Callable[["CommandTicket"], bool]
Compensator = Callable[["CommandTicket"], Awaitable[None]]


@dataclass(frozen=True)
class CommandTicket:
    session_id: str
    epoch: int
    kind: str                     # play / seek / stop / prepare
    command_id: str = field(default_factory=lambda: f"c-{uuid.uuid4().hex[:10]}")


class ManagedExecutor:
    def __init__(
        self,
        *,
        max_pending: int = 16,
        is_current: Ticketer | None = None,
        compensate: Compensator | None = None,
    ) -> None:
        self._max_pending = max(1, int(max_pending))
        self._is_current = is_current or (lambda _ticket: True)
        self._compensate = compensate
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._closed = False
        self.executed = 0
        self.late_dropped = 0
        self.compensated = 0

    async def start(self) -> None:
        if self._worker is None:
            self._queue = asyncio.Queue(maxsize=self._max_pending)
            self._worker = asyncio.ensure_future(self._run())

    async def close(self) -> None:
        self._closed = True
        queue, worker = self._queue, self._worker
        if queue is not None and worker is not None:
            await queue.put(None)
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._queue = None
        self._worker = None

    async def submit(
        self,
        ticket: CommandTicket,
        op: Callable[[], Awaitable[Any]],
    ) -> CommandState:
        await self.start()
        assert self._queue is not None
        if self._queue.full():
            raise QueueFull(f"executor queue full ({self._max_pending})")
        result: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((ticket, op, result))
        return await result

    @property
    def pending(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                break
            ticket, op, result = item
            if result.done():
                continue
            if not self._is_current(ticket):
                self.late_dropped += 1
                result.set_result(CommandState.UNKNOWN)
                continue
            self.executed += 1
            try:
                outcome = await op()
                state = CommandState.FAILED if outcome is False else CommandState.APPLIED
            except Exception:  # noqa: BLE001 - 设备错误按失败回执，不炸执行器
                state = CommandState.FAILED
            # 执行期间代次可能已变（用户 stop / 外部接管）：迟到结果作废，
            # 并做一次补偿 Stop，绝不把结果写回新代次、也不重发命令。
            if not self._is_current(ticket):
                state = CommandState.UNKNOWN
                self.late_dropped += 1
                if self._compensate is not None:
                    try:
                        await self._compensate(ticket)
                        self.compensated += 1
                    except Exception:  # noqa: BLE001 - 补偿失败留给上层回读
                        pass
            if not result.done():
                result.set_result(state)
