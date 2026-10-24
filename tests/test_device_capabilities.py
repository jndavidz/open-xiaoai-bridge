"""设备能力层契约测试（ticket 02）。

只使用 `tests/fake_device.py` 的内存边界：不连真机、不联网、不触 Rust 扩展。
覆盖三件事：

  1. 能力表登记项完整（参数形状 / 成功判据 / 回读 / 超时 / 阻塞 / 幂等）；
  2. 每个能力发出的命令形状正确，判据与回读符合登记表；
  3. 禁区方法无法经能力层调用；业务层不再出现裸 ubus / mphelper 字符串。

同时锁定 `SpeakerManager` 的**现有对外行为**（含 ticket 03 才修的判据缺陷），
确认本次改造是纯 prefactor。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_device import FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import device, ref  # noqa: E402
from core.device import registry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


def _context(status: int = 0, volume: int = 39) -> str:
    return json.dumps(
        {
            "code": 0,
            "info": json.dumps({"status": status, "volume": volume, "player_mode": 1}),
        },
        ensure_ascii=False,
    )


def _tts_response(path: str = "/tmp/tts/tts_abc_123_456.mp3") -> str:
    info = json.dumps({"path": path}, ensure_ascii=False)
    return json.dumps({"code": 0, "info": info}, ensure_ascii=False)


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
        self.caps = device.get_device()
        self.speaker = speaker_module.SpeakerManager()


class RegistryTest(_Base):
    def test_first_batch_capabilities_are_registered(self) -> None:
        names = set(registry.capability_table())
        required = {
            "mic_get",
            "mic_set",
            "volume_get",
            "volume_set",
            "play_url",
            "play_file",
            "pause",
            "tts_synthesize",
            "service_status",
            "system_memory",
            "shell",
        }
        self.assertTrue(required <= names, f"missing: {required - names}")

    def test_every_entry_declares_full_contract(self) -> None:
        for name, spec in registry.capability_table().items():
            self.assertEqual(name, spec.name)
            self.assertTrue(spec.method, name)
            self.assertIsInstance(spec.params, tuple)
            self.assertIsInstance(spec.timeout_ms, (int, float))
            self.assertGreater(spec.timeout_ms, 0)
            self.assertIsInstance(spec.blocking, bool)
            self.assertIsInstance(spec.idempotent, bool)
            self.assertIn(spec.group, {g for g in registry.CapabilityGroup})
            if spec.group is registry.CapabilityGroup.WRITE:
                self.assertIsNotNone(spec.readback, f"{name} write op needs readback")
            if not spec.blocking:
                self.assertLessEqual(spec.timeout_ms, 6000, f"{name} timeout too long")

    def test_readback_names_resolve_to_registered_capabilities(self) -> None:
        for spec in registry.capability_table().values():
            if spec.readback is not None:
                self.assertIn(spec.readback, registry.capability_table(), spec.name)

    def test_forbidden_methods_never_enter_table(self) -> None:
        for prefix, _reason in registry.FORBIDDEN_METHODS:
            for name in registry.capability_table():
                self.assertFalse(
                    name == prefix or name.startswith(prefix + "."),
                    f"forbidden {prefix} leaked into table as {name}",
                )

    def test_forbidden_methods_cannot_be_resolved(self) -> None:
        for name in (
            "miio.miot_rpc",
            "messagingagent.identify_device",
            "messagingagent.set_kv",
            "service.set",
            "service.add",
            "service.delete",
            "service.signal",
            "service.update_config",
            "system.reboot",
            "system.sysupgrade",
            "system.watchdog",
            "player_event",
            "notify.play",
            "notify.create",
            "mediaplayer.set_dlna_status",
        ):
            with self.assertRaises(device.ForbiddenCapabilityError, msg=name):
                registry.resolve_capability(name)

    def test_unknown_capability_is_rejected(self) -> None:
        with self.assertRaises(device.UnknownCapabilityError):
            registry.resolve_capability("no_such_capability")
        with self.assertRaises(device.UnknownCapabilityError):
            _run(self.caps.execute("no_such_capability"))

    def test_shell_guard_blocks_forbidden_objects(self) -> None:
        for script in (
            "ubus call miio miot_rpc '{}'",
            "ubus -t 1 call messagingagent identify_device '{}'",
            "ubus call messagingagent set_kv '{\"key\":\"x\"}'",
            "ubus call service set '{}'",
            "ubus call service update_config '{}'",
            "ubus call system reboot '{}'",
            "ubus call system sysupgrade '{}'",
            "ubus call system watchdog '{}'",
            "ubus call mediaplayer player_event '{}'",
            "ubus call notify create '{\"id\":\"1\"}'",
            "ubus call mediaplayer set_dlna_status '{\"status\":1}'",
        ):
            with self.assertRaises(device.ForbiddenCapabilityError, msg=script):
                registry.ensure_script_allowed(script)
            with self.assertRaises(device.ForbiddenCapabilityError, msg=script):
                _run(self.caps.shell(script))
        self.assertEqual(self.device.calls, [], "forbidden scripts must not reach the device")

    def test_shell_guard_allows_registered_read_paths(self) -> None:
        for script in (
            "ubus call service list '{\"name\":\"mediaplayer\"}'",
            "ubus call system info",
            "ubus call mediaplayer player_get_context",
            "ubus call pnshelper event_notify '{\"src\":3, \"event\":7}' 2>&1",
            "ubus call mediaplayer player_play_url '{\"url\":\"x\",\"type\":1}'",
        ):
            registry.ensure_script_allowed(script)


class MicCapabilityTest(_Base):
    def test_mic_get_reads_mute_file(self) -> None:
        self.device.on("mipns/mute", "on")
        res = _run(self.caps.mic_get())
        self.assertTrue(res.ok)
        self.assertEqual(res.readback["mic"], "on")
        self.device.assert_called("[ ! -f /tmp/mipns/mute ]")

    def test_mic_set_mute_sends_event_7_and_reads_back(self) -> None:
        self.device.on("event_notify", '{"code":0}')
        self.device.on("mipns/mute", "off")
        res = _run(self.caps.mic_set(muted=True))
        script = self.device.matching_calls("event_notify")[0]
        self.assertIn('"src":3, "event":7', script)
        self.assertIn("ubus -t1 -S call pnshelper", script)
        self.assertEqual(res.code, 0)
        self.assertEqual(res.readback["mic"], "off")
        self.assertTrue(res.ok)
        self.device.assert_called("mipns/mute")

    def test_mic_unmute_is_idempotent_and_judged_by_readback(self) -> None:
        # 麦本就开时 event:8 返回 code:-1，这不是失败。
        self.device.on("event_notify", '{"code":-1}')
        self.device.on("mipns/mute", "on")
        res = _run(self.caps.mic_set(muted=False))
        script = self.device.matching_calls("event_notify")[0]
        self.assertIn('"event":8', script)
        self.assertEqual(res.code, -1)
        self.assertTrue(res.readback_ok)
        self.assertTrue(res.ok, res.error)

    def test_mic_set_readback_mismatch_is_failure(self) -> None:
        self.device.on("event_notify", '{"code":0}')
        self.device.on("mipns/mute", "on")  # 要求静音，但文件不存在
        res = _run(self.caps.mic_set(muted=True))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "readback_mismatch")

    def test_speaker_set_mic_keeps_legacy_code_judge(self) -> None:
        self.device.on("event_notify", '{"code":0}')
        self.device.on("mipns/mute", "off")
        self.assertTrue(_run(self.speaker.set_mic(True)))
        self.device.assert_called('"event":7')

        self.device.reset()
        self.device.on("event_notify", '{"code": -1}')
        self.device.on("mipns/mute", "on")
        # 现有缺陷（ticket 03 修）：回读为开麦，但返回码判据给出 False。
        self.assertFalse(_run(self.speaker.set_mic(False)))

    def test_mic_clear_flag_removes_mute_file(self) -> None:
        self.device.on("rm -f", "", exit_code=0)
        self.device.on("mipns/mute", "on")
        res = _run(self.caps.mic_clear_mute_flag())
        self.assertTrue(res.ok)
        self.device.assert_called("rm -f /tmp/mipns/mute")


class VolumeCapabilityTest(_Base):
    def test_volume_get_reads_context_volume(self) -> None:
        self.device.on("player_get_context", _context(volume=39))
        res = _run(self.caps.volume_get())
        self.assertTrue(res.ok)
        self.assertEqual(res.readback["volume"], 39)
        self.device.assert_called("ubus call mediaplayer player_get_context")

    def test_volume_set_sends_level_and_reads_back(self) -> None:
        self.device.on("player_set_volume", '{"code":0}')
        self.device.on("player_get_context", _context(volume=35))
        res = _run(self.caps.volume_set(35))
        self.assertIn("ubus call mediaplayer player_set_volume", self.device.calls[0][0])
        self.assertIn('{"volume": 35}', self.device.calls[0][0])
        self.assertEqual(res.readback["volume"], 35)
        self.assertTrue(res.ok)

    def test_volume_set_readback_mismatch_is_failure(self) -> None:
        self.device.on("player_set_volume", '{"code":0}')
        self.device.on("player_get_context", _context(volume=20))
        res = _run(self.caps.volume_set(35))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "readback_mismatch")


class PlaybackCapabilityTest(_Base):
    def test_play_url_sends_type_and_reads_context(self) -> None:
        self.device.on("player_play_url", '{"code":0}')
        self.device.on("player_get_context", _context(status=1))
        res = _run(self.caps.play_url("http://nas/a.mp3"))
        self.assertIn("ubus call mediaplayer player_play_url", self.device.calls[0][0])
        self.assertIn('"type": 1', self.device.calls[0][0])
        self.assertTrue(res.ok)
        self.assertEqual(res.readback["status"], 1)

    def test_play_url_code_failure_is_failure(self) -> None:
        self.device.on("player_play_url", '{"code":1}')
        self.device.on("player_get_context", _context(status=0))
        res = _run(self.caps.play_url("http://nas/a.mp3"))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "code_nonzero")

    def test_pause_uses_mphelper(self) -> None:
        self.device.on("mphelper pause", '{"code": 0}')
        self.device.on("player_get_context", _context(status=2))
        res = _run(self.caps.pause())
        self.assertEqual(self.device.calls[0][0], "mphelper pause")
        self.assertTrue(res.ok)

    def test_play_file_uses_miplayer_and_exit_code(self) -> None:
        self.device.on("miplayer -f", "", exit_code=0)
        res = _run(self.caps.play_file("/tmp/tts/a.mp3"))
        self.assertIn("miplayer -f '/tmp/tts/a.mp3'", self.device.calls[0][0])
        self.assertTrue(res.ok)

        self.device.reset()
        self.device.on("miplayer -f", "", exit_code=1)
        res = _run(self.caps.play_file("/tmp/tts/a.mp3"))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "exit_code_nonzero")

    def test_play_text_blocking_escapes_single_quotes(self) -> None:
        self.device.on("tts_play.sh", "", exit_code=1)  # tts_play.sh 成功时 rc 也是 1
        res = _run(self.caps.play_text_blocking("it's fine"))
        script = self.device.calls[0][0]
        self.assertTrue(script.startswith("/usr/sbin/tts_play.sh '"))
        self.assertIn("it'\\''s fine", script)

    def test_stop_audio_stops_all_playback_channels(self) -> None:
        self.device.on("killall", '{"code": 0}')
        res = _run(self.caps.stop_audio())
        script = self.device.calls[0][0]
        self.assertIn("killall tts_play.sh miplayer", script)
        self.assertIn("mphelper pause", script)
        self.assertTrue(res.ok)


class TtsCapabilityTest(_Base):
    def test_tts_speak_keeps_save_zero_payload(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        res = _run(self.caps.tts_speak("你好"))
        script = self.device.calls[0][0]
        self.assertIn("ubus call mibrain text_to_speech", script)
        self.assertIn('"save": 0', script)
        self.assertTrue(res.ok)

    def test_tts_synthesize_saves_and_verifies_file(self) -> None:
        self.device.on("text_to_speech", _tts_response())
        self.device.on("[ -f", "yes")
        res = _run(self.caps.tts_synthesize("静默测试"))
        script = self.device.calls[0][0]
        self.assertIn('"save": 1', script)
        self.assertIn('"play": 0', script)
        self.assertEqual(res.readback["path"], "/tmp/tts/tts_abc_123_456.mp3")
        self.assertTrue(res.readback_ok)
        self.assertTrue(res.ok)
        self.device.assert_called("[ -f '/tmp/tts/tts_abc_123_456.mp3' ]")

    def test_tts_synthesize_missing_path_is_failure(self) -> None:
        self.device.on("text_to_speech", '{"code":0,"info":"{}"}')
        res = _run(self.caps.tts_synthesize("静默测试"))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "path_missing")

    def test_tts_synthesize_file_not_created_is_failure(self) -> None:
        self.device.on("text_to_speech", _tts_response())
        self.device.on("[ -f", "no")
        res = _run(self.caps.tts_synthesize("静默测试"))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "readback_mismatch")


class HealthCapabilityTest(_Base):
    def test_service_status_reads_procd_running(self) -> None:
        self.device.on(
            "service list",
            json.dumps(
                {
                    "mediaplayer": {
                        "instances": {
                            "instance1": {"running": True, "pid": 1234},
                        }
                    }
                }
            ),
        )
        res = _run(self.caps.service_status())
        self.assertTrue(res.ok)
        self.assertTrue(res.readback["running"])
        self.assertEqual(res.readback["pid"], 1234)

    def test_service_status_not_running(self) -> None:
        self.device.on(
            "service list",
            json.dumps({"mediaplayer": {"instances": {"instance1": {"running": False}}}}),
        )
        res = _run(self.caps.service_status())
        self.assertTrue(res.ok)
        self.assertFalse(res.readback["running"])

    def test_system_memory_reads_free_bytes(self) -> None:
        self.device.on(
            "system info",
            json.dumps(
                {
                    "uptime": 571,
                    "memory": {"total": 121610240, "free": 7409664},
                    "swap": {"total": 0, "free": 0},
                }
            ),
        )
        res = _run(self.caps.system_memory())
        self.assertTrue(res.ok)
        self.assertEqual(res.readback["free"], 7409664)
        self.assertEqual(res.readback["total"], 121610240)

    def test_timeout_is_explicit_failure_not_hang(self) -> None:
        self.device.on("player_get_context", timeout=True)
        res = _run(self.caps.volume_get())
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "timeout")
        self.assertEqual(res.request.stdout, "error")
        self.assertEqual(res.request.exit_code, -1)

    def test_unparsable_device_response_is_failure(self) -> None:
        self.device.on("system info", "not json")
        res = _run(self.caps.system_memory())
        self.assertFalse(res.ok)
        self.assertIn(res.error, {"json_invalid", "invalid_response"})


class SpeakerFacadeTest(_Base):
    """对外方法签名与现有行为必须保持不变（含已知判据缺陷）。"""

    def test_play_blocking_text_keeps_exit_code_judge(self) -> None:
        self.device.on("tts_play.sh", "", exit_code=1)
        self.assertFalse(_run(self.speaker.play(text="你好", blocking=True)))

        self.device.reset()
        self.device.on("tts_play.sh", "", exit_code=0)
        self.assertTrue(_run(self.speaker.play(text="你好", blocking=True)))

    def test_play_blocking_url_uses_miplayer(self) -> None:
        self.device.on("miplayer", "", exit_code=0)
        self.assertTrue(_run(self.speaker.play(url="http://nas/a.mp3", blocking=True)))
        self.assertIn("miplayer -f 'http://nas/a.mp3'", self.device.last_script)

    def test_play_nonblocking_uses_code_substring_judge(self) -> None:
        self.device.on("player_play_url", '{"code": 0}')
        self.device.on("player_get_context", _context(status=1))
        self.assertTrue(
            _run(self.speaker.play(url="http://nas/a.mp3", blocking=False))
        )

        self.device.reset()
        self.device.on("text_to_speech", '{"code": 1}')
        self.assertFalse(_run(self.speaker.play(text="你好", blocking=False)))

    def test_get_playing_and_set_playing_unchanged(self) -> None:
        self.device.on("mute_stat", "1")
        self.assertEqual(_run(self.speaker.get_playing(sync=True)), "playing")

        self.device.reset()
        self.device.on("mute_stat", "2")
        self.assertEqual(_run(self.speaker.get_playing(sync=True)), "paused")

        self.device.reset()
        self.device.on("mphelper play", '{"code": 0}')
        self.device.on("player_get_context", _context(status=1))
        self.assertTrue(_run(self.speaker.set_playing(True)))
        self.assertEqual(self.device.calls[0][0], "mphelper play")

    def test_stop_device_audio_sequence(self) -> None:
        self.device.on("killall", '{"code": 0}')
        _run(self.speaker.stop_device_audio())
        self.assertIn("killall tts_play.sh miplayer", self.device.calls[0][0])

    def test_wake_sleep_and_ask_keep_shapes(self) -> None:
        self.device.on("event_notify", '{"code": 0}')
        self.assertTrue(_run(self.speaker.wake_up(awake=True, silent=True)))
        self.assertIn('{"src":1,"event":0}', self.device.calls[0][0])

        self.device.reset()
        self.device.on("event_notify", '{"code": 0}')
        self.assertTrue(_run(self.speaker.wake_up(awake=True, silent=False)))
        self.assertIn('{"src":0,"event":0}', self.device.calls[0][0])

        self.device.reset()
        self.device.on("event_notify", '{"code": 0}')
        self.assertTrue(_run(self.speaker.wake_up(awake=False)))
        script = self.device.calls[0][0]
        self.assertIn('"event":7', script)
        self.assertIn("sleep 0.1", script)
        self.assertIn('"event":8', script)

        self.device.reset()
        self.device.on("ai_service", '{"code": 0}')
        self.assertTrue(_run(self.speaker.ask_xiaoai("打开客厅灯")))
        self.assertIn("ubus call mibrain ai_service", self.device.calls[0][0])

    def test_device_info_and_boot_partition_controls(self) -> None:
        self.device.on("micocfg_model", "LX06 SN123")
        info = _run(self.speaker.get_device())
        self.assertEqual(info, {"model": "LX06", "sn": "SN123"})

        self.device.reset()
        self.device.on("fw_env -g", "boot1")
        self.assertEqual(_run(self.speaker.get_boot()), "boot1")

        self.device.reset()
        self.device.on("fw_env -g", "boot1")
        self.device.on("fw_env -s", "boot1")
        self.assertTrue(_run(self.speaker.set_boot("boot1")))

    def test_run_shell_still_returns_command_result(self) -> None:
        self.device.on("echo hi", "hi")
        res = _run(self.speaker.run_shell("echo hi"))
        self.assertEqual(res.stdout, "hi")
        self.assertEqual(res.exit_code, 0)

    def test_business_layer_has_no_raw_device_commands(self) -> None:
        device_dir = ROOT / "core" / "device"
        files = [ROOT / "config.py", ROOT / "main.py"]
        files.extend(
            path
            for path in (ROOT / "core").rglob("*.py")
            if device_dir not in path.parents
        )
        for path in files:
            source = path.read_text(encoding="utf-8")
            for token in ("ubus call", "mphelper", "miplayer", "tts_play.sh"):
                self.assertNotIn(
                    token, source, f"{path.relative_to(ROOT)} still contains {token!r}"
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
