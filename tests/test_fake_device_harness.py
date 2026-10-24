"""Self-tests for the fake device transport (ticket 01).

These tests are intentionally dependency-free: stdlib unittest only. They
verify the harness itself, plus the first contract test against SpeakerManager
so later tickets can build on it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_device import FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import ref  # noqa: E402


class FakeDeviceHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        ref.GLOBAL_STATES.clear()
        self.device = FakeDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        self.speaker = speaker_module.SpeakerManager()

    def test_get_mic_returns_on_when_mute_file_absent(self) -> None:
        self.device.on("mipns/mute", "on")
        self.assertEqual(_run(self.speaker.get_mic()), "on")
        self.device.assert_called("mipns/mute")

    def test_get_mic_returns_off_by_default(self) -> None:
        self.device.set_default(stdout="")
        self.assertEqual(_run(self.speaker.get_mic()), "off")

    def test_set_playing_true_only_for_code_zero(self) -> None:
        self.device.on("mphelper play", '{"code": 0}')
        self.assertTrue(_run(self.speaker.set_playing(True)))

        self.device.reset()
        self.device.on("mphelper play", '{"code": 1}')
        self.assertFalse(_run(self.speaker.set_playing(True)))

    def test_device_timeout_becomes_explicit_failure(self) -> None:
        self.device.on("mphelper pause", timeout=True)
        self.assertFalse(_run(self.speaker.set_playing(False)))

    def test_get_device_parses_model_and_sn(self) -> None:
        self.device.on("micocfg_model", "LX06 SN123456")
        info = _run(self.speaker.get_device())
        self.assertEqual(info["model"], "LX06")
        self.assertEqual(info["sn"], "SN123456")

    def test_command_log_records_script_and_timeout(self) -> None:
        self.device.on("mphelper play", '{"code": 0}')
        _run(self.speaker.set_playing(True))
        self.assertEqual(len(self.device.calls), 1)
        script, timeout = self.device.calls[0]
        self.assertIn("mphelper play", script)
        self.assertGreater(timeout, 0)

    def test_matching_rules_are_last_registered_first(self) -> None:
        self.device.on("echo", "first")
        self.device.on("echo", "second")
        self.assertIn("second", _run(self.device.run_shell("echo hi")))

    def test_assert_called_reports_actual_commands(self) -> None:
        self.device.on("one", "ok")
        _run(self.device.run_shell("one"))
        self.device.assert_called("one")
        with self.assertRaises(AssertionError) as ctx:
            self.device.assert_called("two")
        self.assertIn("one", str(ctx.exception))

    def test_events_and_audio_are_replayable(self) -> None:
        _run(self.device.emit_event("playing", "playing"))
        self.device.feed_audio(b"\x00\x01")
        self.assertEqual(len(self.device.events), 1)
        self.assertEqual(self.device.audio_frames, [b"\x00\x01"])


def _run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
