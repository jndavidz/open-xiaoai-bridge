"""托管播放运行期（B-06 接线，默认关闭）。

只做一件事：按固定间隔驱动 `ManagedQueuePlayer.tick()`；到点推进后自动起播
下一条。单设备语义 ⇒ 同一时刻只绑定一个 session（bridge 只有一个目标音箱）。

默认不启动：`managed.enabled`（config）或 `MANAGED_ENABLE=1` 才由 APIServer 拉起。
"""

from __future__ import annotations

import asyncio

from core.music.player import Decision, ManagedQueuePlayer
from core.utils.logger import logger

_RUNTIME: "ManagedRuntime | None" = None


class ManagedRuntime:
    def __init__(self, player: ManagedQueuePlayer, *, service=None,
                 interval_s: float = 1.0) -> None:
        self.player = player
        self.service = service
        self.interval_s = max(0.001, float(interval_s))
        self.session_id: str | None = None
        self._task: asyncio.Task | None = None
        self.ticks = 0

    def bind(self, session_id: str | None) -> None:
        self.session_id = session_id

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def tick_once(self) -> Decision | None:
        """驱动一次；推进后立刻起播下一条（末条则由 player 自己置 stopped）。"""

        if not self.session_id:
            return None
        self.ticks += 1
        decision = await self.player.tick(self.session_id)
        if decision is Decision.ADVANCE:
            await self.player.start(self.session_id)
        return decision

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.tick_once()
            except Exception as exc:  # noqa: BLE001 - 运行期异常不得杀循环
                logger.warning(f"[ManagedRuntime] tick failed: {type(exc).__name__}: {exc}")


def get_runtime() -> ManagedRuntime | None:
    return _RUNTIME


def set_runtime(runtime: ManagedRuntime | None) -> None:
    global _RUNTIME
    _RUNTIME = runtime
