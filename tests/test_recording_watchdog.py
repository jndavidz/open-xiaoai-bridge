"""录音看门狗状态机纯逻辑测试（ticket 04）。

Stdlib only，不连真机、不联网。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.watchdog import (  # noqa: E402
    STATE_IDLE,
    STATE_RECOVER,
    STATE_WARNING,
    RecordingWatchdog,
)


class RecordingWatchdogTest(unittest.TestCase):
    def setUp(self):
        self.watchdog = RecordingWatchdog(timeout_seconds=1.0)

    def test_idle_when_frames_are_fresh(self):
        state = self.watchdog.evaluate(last_frame_time=10.0, now=10.2, expected=True)
        self.assertEqual(STATE_IDLE, state)

    def test_warning_between_half_and_full_timeout(self):
        state = self.watchdog.evaluate(last_frame_time=10.0, now=10.6, expected=True)
        self.assertEqual(STATE_WARNING, state)

    def test_recover_at_timeout(self):
        state = self.watchdog.evaluate(last_frame_time=10.0, now=11.0, expected=True)
        self.assertEqual(STATE_RECOVER, state)

    def test_recover_when_expected_but_no_frame_seen(self):
        state = self.watchdog.evaluate(last_frame_time=None, now=10.0, expected=True)
        self.assertEqual(STATE_RECOVER, state)

    def test_expected_false_never_recovers(self):
        """TTS 停麦 / 注入窗口等预期停止场景不得误恢复。"""
        state = self.watchdog.evaluate(last_frame_time=10.0, now=100.0, expected=False)
        self.assertEqual(STATE_IDLE, state)
        self.assertEqual(0, self.watchdog.recover_count)

    def test_expected_false_clears_latched_recover_state(self):
        self.assertEqual(
            STATE_RECOVER,
            self.watchdog.evaluate(last_frame_time=10.0, now=20.0, expected=True),
        )
        self.assertEqual(
            STATE_IDLE,
            self.watchdog.evaluate(last_frame_time=10.0, now=20.1, expected=False),
        )

    def test_notify_recovered_increments_count_and_resets(self):
        self.watchdog.evaluate(last_frame_time=10.0, now=12.0, expected=True)
        self.assertEqual(1, self.watchdog.notify_recovered())
        self.assertEqual(1, self.watchdog.recover_count)
        self.assertEqual(STATE_IDLE, self.watchdog.state)
        self.watchdog.evaluate(last_frame_time=12.0, now=20.0, expected=True)
        self.assertEqual(2, self.watchdog.notify_recovered())

    def test_mark_started_resets_state(self):
        self.watchdog.evaluate(last_frame_time=10.0, now=20.0, expected=True)
        self.assertEqual(STATE_RECOVER, self.watchdog.state)
        self.watchdog.mark_started()
        self.assertEqual(STATE_IDLE, self.watchdog.state)

    def test_configure_updates_threshold(self):
        self.watchdog.configure(2.0)
        self.assertEqual(
            STATE_IDLE,
            self.watchdog.evaluate(last_frame_time=10.0, now=10.5, expected=True),
        )
        self.assertEqual(
            STATE_WARNING,
            self.watchdog.evaluate(last_frame_time=10.0, now=11.5, expected=True),
        )
        self.assertEqual(
            STATE_RECOVER,
            self.watchdog.evaluate(last_frame_time=10.0, now=12.0, expected=True),
        )

    def test_one_second_threshold_is_strictly_below_legacy_three_seconds(self):
        self.assertEqual(1.0, RecordingWatchdog().timeout_seconds)
        self.assertLess(RecordingWatchdog().timeout_seconds, 3.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
