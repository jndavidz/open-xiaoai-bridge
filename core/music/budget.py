"""端到端重试预算（B-05）。

规则：**最外层命令持有一份预算**，下游（bridge/Songloft/插件/kwqq）只能
消耗剩余时间，不能各自新增或叠加。子层拿到的是「同一 deadline 的视图」，
因此子层耗尽 ⇔ 上层也已耗尽；任何一层都不允许再开新的预算窗口。

时间用注入的时钟，测试可完全离线。
"""

from __future__ import annotations

import time

from core.music.model import ManagedError


class BudgetExhausted(ManagedError):
    """预算已耗尽：调用方应把控制权交回最外层，而不是自己重试。"""


class RetryBudget:
    def __init__(self, total_ms: float, *, clock=time.monotonic) -> None:
        self.total_ms = max(0.0, float(total_ms))
        self._clock = clock
        self._deadline = self._clock() + self.total_ms / 1000.0

    @property
    def remaining_ms(self) -> int:
        return max(0, int(round((self._deadline - self._clock()) * 1000.0)))

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0

    def child(self) -> "RetryBudget":
        """下游视图：共享同一 deadline，**不新增**预算。"""

        child = RetryBudget(0.0, clock=self._clock)
        child._deadline = self._deadline
        child.total_ms = self.total_ms
        return child

    def ensure(self) -> None:
        if self.expired:
            raise BudgetExhausted(f"retry budget exhausted ({self.total_ms:.0f}ms)")

    def can_retry(self) -> bool:
        """只允许最外层在剩余预算内做一次受控重试。"""

        return not self.expired
