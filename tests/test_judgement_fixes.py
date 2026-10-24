"""判据类缺陷修复（ticket 03）的契约测试。

全部走 `tests/fake_device.py` 的内存设备边界：不连真机、不联网、不触 Rust 扩展。

覆盖：
  1. 阻塞播报：合成落盘 → file:// 播放 → 回读空闲 → 清理；合成失败回退设备脚本；
     含引号/反斜杠文本回退不破 JSON；播放完成不可观测时超时兜底；回读失败判失败。
  2. 音量：百分比通道设置 + player_get_context 回读；钳位；回读不一致判失败。
  3. 麦克风：`set_mic(on=True)` 发 event:8 并回读；闭麦发 event:7；幂等返错不判失败。
  4. 存活与内存：procd service list 判 running；system info memory.free；读不到返回 None。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_device import CommandResult, FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import ref  # noqa: E402
from core.device import registry  # noqa: E402

TTS_PATH = "/tmp/tts/tts_deadbeef_1700000000_123456.mp3"


def _run(coro):
    return asyncio.run(coro)


def _context(status: int = 0, volume: int = 39) -> CommandResult:
    return CommandResult(
        json.dumps(
            {
                "code": 0,
                "info": json.dumps(
                    {"status": status, "volume": volume, "player_mode": 1},
                    ensure_ascii=False,
                ),
            },
            ensure_ascii=False,
        )
    )


def _synth(path: str = TTS_PATH) -> CommandResult:
    info = json.dumps({"path": path}, ensure_ascii=False)
    return CommandResult(json.dumps({"code": 0, "info": info}, ensure_ascii=False))


def _shell_arg(script: str) -> str:
    """取出 tts_play.sh 的单引号参数并还原 shell 转义。"""

    prefix = "/usr/sbin/tts_play.sh "
    assert script.startswith(prefix), script
    inner = script[len(prefix) :]
    assert inner.startswith("'") and inner.endswith("'"), inner
    return inner[1:-1].replace("'\\''", "'")


def _tts_script_calls(device: FakeDevice) -> list[str]:
    return device.matching_calls("tts_play.sh")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        import open_xiaoai_server  # 由 load_speaker_module 注入的桩

        async def _noop(*_args, **_kwargs):
            return None

        for name in ("stop_playing", "play_audio_file"):
            if not hasattr(open_xiaoai_server, name):
                setattr(open_xiaoai_server, name, _noop)

        ref.GLOBAL_STATES.clear()
        self.device = FakeDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        self.speaker = speaker_module.SpeakerManager()

        # 轮询/宽限压到毫秒级，避免测试真的等 1s 宽限。
        self._saved_poll = speaker_module._PLAYBACK_POLL_INTERVAL_MS
        self._saved_grace = speaker_module._PLAYBACK_START_GRACE_MS
        speaker_module._PLAYBACK_POLL_INTERVAL_MS = 2
        speaker_module._PLAYBACK_START_GRACE_MS = 10

    def tearDown(self) -> None:
        speaker_module._PLAYBACK_POLL_INTERVAL_MS = self._saved_poll
        speaker_module._PLAYBACK_START_GRACE_MS = self._saved_grace


class BlockingSpeechTest(_Base):
    def test_normal_path_synthesizes_plays_file_and_cleans_up(self) -> None:
        self.device.on("text_to_speech", _synth().stdout)
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code":0}')
        self.device.on_sequence(
            "player_get_context", [_context(0), _context(1), _context(0)]
        )

        ok = _run(self.speaker.play(text="你好", blocking=True, timeout=500))

        self.assertTrue(ok)
        scripts = [script for script, _ in self.device.calls]
        synth = [s for s in scripts if "text_to_speech" in s][0]
        self.assertIn('"save": 1', synth)
        self.assertIn('"play": 0', synth)
        play = [s for s in scripts if "player_play_url" in s][0]
        self.assertIn(f'"url": "file://{TTS_PATH}"', play)
        self.assertIn('"type": 1', play)
        self.assertTrue(
            any(s.startswith("rm -f ") and TTS_PATH in s for s in scripts),
            scripts,
        )
        # 默认路径不碰设备脚本
        self.assertEqual(_tts_script_calls(self.device), [])

    def test_synthesis_failure_falls_back_to_device_script(self) -> None:
        self.device.on("text_to_speech", '{"code":1}')
        self.device.on("tts_play.sh", "", exit_code=1)  # 成功时 rc 也可能是 1
        self.device.on("player_get_context", _context(0).stdout)

        ok = _run(self.speaker.play(text="你好", blocking=True, timeout=200))

        self.assertTrue(ok, "tts_play.sh 的 rc 不得作为成败判据")
        self.assertEqual(len(_tts_script_calls(self.device)), 1)
        self.device.assert_not_called("player_play_url")
        self.device.assert_not_called("rm -f")

    def test_fallback_escapes_quotes_and_backslashes_without_breaking_json(self) -> None:
        text = '他说"你好"然后 C:\\temp 走了\n换行'
        self.device.on("text_to_speech", '{"code":1}')
        self.device.on("tts_play.sh", "", exit_code=1)
        self.device.on("player_get_context", _context(0).stdout)

        ok = _run(self.speaker.play(text=text, blocking=True, timeout=200))

        self.assertTrue(ok)
        script = _tts_script_calls(self.device)[0]
        inner = _shell_arg(script)
        self.assertIn('\\"', inner)
        self.assertIn("\\\\", inner)
        # 还原后的参数嵌入 tts_play.sh 的 JSON 字面量必须仍可解析且文本不丢。
        payload = json.loads('{"text": "' + inner + '", "save": 1}')
        self.assertEqual(payload["text"], text)

    def test_fallback_handles_single_quotes_and_double_quotes_together(self) -> None:
        text = "it's a \"quote\""
        self.device.on("text_to_speech", '{"code":1}')
        self.device.on("tts_play.sh", "", exit_code=1)
        self.device.on("player_get_context", _context(0).stdout)

        self.assertTrue(_run(self.speaker.play(text=text, blocking=True, timeout=200)))
        inner = _shell_arg(_tts_script_calls(self.device)[0])
        self.assertIn('\\"', inner)
        payload = json.loads('{"text": "' + inner + '", "save": 1}')
        self.assertEqual(payload["text"], text)

    def test_play_command_failure_is_explicit_failure(self) -> None:
        self.device.on("text_to_speech", _synth().stdout)
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code":1}')
        self.device.on("player_get_context", _context(0).stdout)

        ok = _run(self.speaker.play(text="你好", blocking=True, timeout=200))

        self.assertFalse(ok)
        self.device.assert_called("rm -f")
        self.assertEqual(_tts_script_calls(self.device), [], "只在合成失败时回退")

    def test_missing_completion_event_falls_back_to_timeout(self) -> None:
        self.device.on("text_to_speech", _synth().stdout)
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code":0}')
        self.device.on("player_get_context", _context(1).stdout)  # 永远 playing

        started = time.monotonic()
        ok = _run(self.speaker.play(text="你好", blocking=True, timeout=80))
        elapsed = time.monotonic() - started

        self.assertTrue(ok, "超时是完成判据的兜底，不是失败")
        self.assertLess(elapsed, 2.0)
        self.device.assert_called("rm -f")

    def test_readback_failure_is_explicit_failure(self) -> None:
        self.device.on("text_to_speech", _synth().stdout)
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code":0}')
        self.device.on("player_get_context", "not json")

        ok = _run(self.speaker.play(text="你好", blocking=True, timeout=80))

        self.assertFalse(ok)
        self.device.assert_called("rm -f")

    def test_cleanup_skips_paths_outside_tmp_tts(self) -> None:
        self.device.on("text_to_speech", _synth("/data/etc/secret.mp3").stdout)
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code":0}')
        self.device.on("player_get_context", _context(0).stdout)

        ok = _run(self.speaker.play(text="你好", blocking=True, timeout=80))

        self.assertTrue(ok)
        self.device.assert_not_called("rm -f")

    def test_capability_refuses_to_remove_non_tts_path(self) -> None:
        with self.assertRaises(ValueError):
            _run(self.speaker.device.tts_cleanup("/data/etc/passwd"))
        self.device.assert_not_called("rm -f")

    def test_playback_budget_estimates_by_text_and_keeps_caller_cap(self) -> None:
        short = speaker_module.estimate_playback_budget_ms("你好", 600_000)
        long = speaker_module.estimate_playback_budget_ms("字" * 200, 600_000)
        self.assertGreaterEqual(short, 8_000)
        self.assertGreater(long, short)
        self.assertLessEqual(long, 120_000)
        self.assertEqual(speaker_module.estimate_playback_budget_ms("你好", 300), 300)


class VolumeTest(_Base):
    def test_set_volume_uses_percent_channel_and_reads_back(self) -> None:
        self.device.on("player_set_continuous_volume", '{"code":0}')
        self.device.on("player_get_context", _context(volume=35).stdout)

        self.assertTrue(_run(self.speaker.set_volume(35)))

        script = self.device.matching_calls("player_set_continuous_volume")[0]
        self.assertIn('"volume": 35', script)
        self.assertIn('"media": "mediaplayer"', script)
        self.device.assert_not_called("player_set_volume")
        self.assertEqual(_run(self.speaker.get_volume()), 35)

    def test_set_volume_clamps_to_device_range(self) -> None:
        self.device.on("player_set_continuous_volume", '{"code":0}')
        self.device.on("player_get_context", _context(volume=100).stdout)

        self.assertTrue(_run(self.speaker.set_volume(150)))
        script = self.device.matching_calls("player_set_continuous_volume")[0]
        self.assertIn('"volume": 100', script)

    def test_set_volume_readback_mismatch_is_false(self) -> None:
        self.device.on("player_set_continuous_volume", '{"code":0}')
        self.device.on("player_get_context", _context(volume=20).stdout)

        self.assertFalse(_run(self.speaker.set_volume(35)))

    def test_get_volume_returns_minus_one_when_readback_fails(self) -> None:
        self.device.on("player_get_context", "not json")
        self.assertEqual(_run(self.speaker.get_volume()), -1)

    def test_volume_capability_registered_on_percent_channel(self) -> None:
        spec = registry.capability_table()["volume_set"]
        self.assertIn("player_set_continuous_volume", spec.method)
        self.assertEqual(spec.readback, "volume_get")


class MicTest(_Base):
    def test_set_mic_on_sends_event_8_and_judges_by_readback(self) -> None:
        # 麦本就开：event:8 返回 code:-1（幂等），回读为 on ⇒ 成功。
        self.device.on("event_notify", '{"code":-1}')
        self.device.on("mipns/mute", "on")

        self.assertTrue(_run(self.speaker.set_mic(on=True)))
        script = self.device.matching_calls("event_notify")[0]
        self.assertIn('"event":8', script)
        self.device.assert_called("mipns/mute")

    def test_set_mic_off_sends_event_7(self) -> None:
        self.device.on("event_notify", '{"code":0}')
        self.device.on("mipns/mute", "off")

        self.assertTrue(_run(self.speaker.set_mic(on=False)))
        self.assertIn('"event":7', self.device.matching_calls("event_notify")[0])

    def test_set_mic_reports_false_on_readback_mismatch(self) -> None:
        self.device.on("event_notify", '{"code":0}')
        self.device.on("mipns/mute", "off")  # 要求开麦，但设备仍静音

        self.assertFalse(_run(self.speaker.set_mic(on=True)))
        self.assertEqual(_run(self.speaker.get_mic()), "off")

    def test_get_mic_unchanged(self) -> None:
        self.device.on("mipns/mute", "on")
        self.assertEqual(_run(self.speaker.get_mic()), "on")

        self.device.reset()
        self.device.on("mipns/mute", "off")
        self.assertEqual(_run(self.speaker.get_mic()), "off")


class HealthProbeTest(_Base):
    def test_mediaplayer_alive_true_when_procd_running(self) -> None:
        self.device.on(
            "service list",
            json.dumps(
                {"mediaplayer": {"instances": {"instance1": {"running": True, "pid": 7}}}}
            ),
        )
        self.assertTrue(_run(self.speaker.mediaplayer_alive()))

    def test_mediaplayer_alive_false_when_service_gone(self) -> None:
        self.device.on(
            "service list",
            json.dumps({"mediaplayer": {"instances": {"instance1": {"running": False}}}}),
        )
        self.assertFalse(_run(self.speaker.mediaplayer_alive()))

    def test_mediaplayer_alive_none_when_probe_unreadable(self) -> None:
        self.device.on("service list", timeout=True)
        self.assertIsNone(_run(self.speaker.mediaplayer_alive()))

    def test_memory_free_reads_system_info(self) -> None:
        self.device.on(
            "system info",
            json.dumps({"memory": {"total": 121610240, "free": 7409664}}),
        )
        self.assertEqual(_run(self.speaker.memory_free()), 7409664)

    def test_memory_free_none_when_unreadable(self) -> None:
        self.device.on("system info", "not json")
        self.assertIsNone(_run(self.speaker.memory_free()))

        self.device.reset()
        self.device.on("system info", timeout=True)
        self.assertIsNone(_run(self.speaker.memory_free()))


class RegistryMetadataTest(unittest.TestCase):
    def test_tts_cleanup_registered_with_prefix_guard_note(self) -> None:
        spec = registry.capability_table()["tts_cleanup"]
        self.assertIn("rm -f", spec.method)
        self.assertTrue(spec.idempotent)

    def test_blocking_script_is_documented_as_fallback(self) -> None:
        spec = registry.capability_table()["play_text_blocking"]
        self.assertTrue(any("兜底" in note for note in spec.notes), spec.notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
