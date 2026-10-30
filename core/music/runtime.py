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
                 interval_s: float = 1.0, max_start_attempts: int = 8) -> None:
        self.player = player
        self.service = service
        self.interval_s = max(0.001, float(interval_s))
        self.max_start_attempts = max(1, int(max_start_attempts))
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
        """驱动一次；推进后立刻起播下一条（末条则由 player 自己置 stopped）。

        起播失败（解析 415/准备失败/设备拒绝）**不得卡在 preparing**：
        记录 `advance_failed` 事件→跳过该条继续；没有下一条就以
        `stop_reason=advance_failed` 可见停止。跳过的条数不超过队列长度。
        """

        sid = self.session_id
        if not sid:
            return None
        self.ticks += 1
        decision = await self.player.tick(sid)
        if decision is not Decision.ADVANCE:
            return decision
        if not await self.start_current(sid):
            # 只解绑：在 _loop 任务内部 await self.stop() 会取消并等待自身
            # （Task cannot await on itself），随后被 except 吞掉，留下
            # “_task=None 但旧任务还活着”的双循环隐患。
            # 只解绑**本 tick 开始时那个会话**：期间可能已切到新会话。
            if self.session_id == sid:
                self.bind(None)
            return Decision.FINISH
        return decision

    async def start_current(self, session_id: str) -> bool:
        """起播当前项；失败按有界次数跳过，末项失败由 `skip_failed` 落成可见停止。

        tick（推进后起播）与 HTTP/语音的**首次 play 命令**必须共用这一条：两处各写一
        遍「失败必须可见」就会漏掉一侧——2026-10-30 E1 覆盖面实测：命令路径的 prepare
        超时会话永久停在 `preparing`（runtime 那时还没 start，没有 tick 来兜底）。
        """

        for _ in range(self.max_start_attempts):
            try:
                await self.player.start(session_id)
                return True
            except Exception as exc:  # noqa: BLE001 - 单条失败不能让会话静默卡死
                logger.warning(f"[ManagedRuntime] start failed: {type(exc).__name__}: {exc}")
                if not self.player.skip_failed(session_id, str(exc)):
                    return False
        return False

    async def _loop(self) -> None:
        while self.session_id:
            await asyncio.sleep(self.interval_s)
            if not self.session_id:
                break
            try:
                await self.tick_once()
            except Exception as exc:  # noqa: BLE001 - 运行期异常不得杀循环
                logger.warning(f"[ManagedRuntime] tick failed: {type(exc).__name__}: {exc}")


def get_runtime() -> ManagedRuntime | None:
    return _RUNTIME


def set_runtime(runtime: ManagedRuntime | None) -> None:
    global _RUNTIME
    _RUNTIME = runtime
