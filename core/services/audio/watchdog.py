"""录音通道看门狗（纯状态机，无 I/O、无第三方依赖）。

背景（baseline §5）：client 的录音读循环在 **500ms 无数据**时自行退出并
`stop_recording`，**不通知服务端**；旧实现下录音静默死亡后唤醒词永久失效。
因此服务端必须在「期望录音」时观察最后一帧时间，连续 `timeout` 秒无帧即
判定死亡并执行「先停后启」。阈值默认 1.0s，显著小于原来的 3s。

本模块只做判定，不执行恢复；恢复动作（stop/start + 退避）在 `core/xiaoai.py`。
"""

from __future__ import annotations

STATE_IDLE = "idle"
STATE_WARNING = "warning"
STATE_RECOVER = "recover"


class RecordingWatchdog:
    """输入「最后一帧时间 / 现在 / 是否期望录音」，输出 idle/warning/recover。

    - `expected=False`（TTS 停麦、注入窗口等预期停止）时永远返回 idle，
      不会误触发恢复；`expected` 必须由 `XiaoAI.start_recording/stop_recording`
      包装维护。
    - 从未收到过帧（`last_frame_time=None`）且期望录音 ⇒ 直接 recover。
    - 恢复成功调用 `notify_recovered()`：计数 +1 并回到 idle。
    """

    def __init__(self, timeout_seconds: float = 1.0, warning_ratio: float = 0.5) -> None:
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.warning_ratio = min(max(0.0, float(warning_ratio)), 1.0)
        self.recover_count = 0
        self.state = STATE_IDLE

    def configure(self, timeout_seconds: float) -> None:
        self.timeout_seconds = max(0.1, float(timeout_seconds))

    def reset(self) -> None:
        self.recover_count = 0
        self.state = STATE_IDLE

    def evaluate(self, last_frame_time: float | None, now: float, expected: bool) -> str:
        if not expected:
            self.state = STATE_IDLE
            return self.state

        if last_frame_time is None:
            self.state = STATE_RECOVER
            return self.state

        age = max(0.0, float(now) - float(last_frame_time))
        if age >= self.timeout_seconds:
            self.state = STATE_RECOVER
        elif age >= self.timeout_seconds * self.warning_ratio:
            self.state = STATE_WARNING
        else:
            self.state = STATE_IDLE
        return self.state

    def mark_started(self) -> None:
        """录音（重新）启动：清掉上一轮的告警/恢复状态。"""
        self.state = STATE_IDLE

    def notify_recovered(self) -> int:
        self.recover_count += 1
        self.state = STATE_IDLE
        return self.recover_count
