"""播报期间固件级闭麦闸门（ticket 10）的契约测试。

全部走 `tests/fake_device.py` 的内存设备边界：不连真机、不联网、不触 Rust 扩展。
`MuteAwareDevice` 在 FakeDevice 之上模拟设备真值 `/tmp/mipns/mute`：
`event:7` 创建（闭麦）、`event:8` 删除（开麦）、`mic_get` 读文件存在性。

覆盖：
  1. 快照 on → 播报期 off → 播完 on；
  2. 快照 off（用户「停止聆听」）→ 持有期与退出后都保持 off，绝不无条件开麦；
  3. 播报异常仍恢复快照；
  4. 快照读取失败的 fallback（KWS listen_disabled true/false）；
  5. 回读失败只告警，不阻断播报；
  6. 持有超上限 → 告警并强制恢复快照；
  7. 启动期 reconcile：残留闭麦清掉，但「停止聆听」时保持 off；
  8. 原有 set_mic/get_mic 语义回归。
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

from fake_device import CommandResult, FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import ref  # noqa: E402

EVENT_7 = '"event":7'
EVENT_8 = '"event":8'
MIC_FILE = "mipns/mute"


def _run(coro):
    return asyncio.run(coro)


class _FakeKWS:
    """KWS 的最小替身：只暴露「停止聆听」开关。"""

    def __init__(self, disabled: bool = False) -> None:
        self.listen_disabled = disabled

    def is_listening_disabled(self) -> bool:
        return self.listen_disabled


class MuteAwareDevice(FakeDevice):
    """FakeDevice + 内存版 /tmp/mipns/mute 状态机。

    `fail_reads` 让接下来 N 次麦态读取抛 TimeoutError（模拟回读失败）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.muted = False
        self.fail_reads = 0

    async def run_shell(self, script: str, timeout: float = 10_000) -> str:
        if EVENT_7 in script or '"event": 7' in script:
            self.calls.append((script, timeout))
            self.muted = True
            return CommandResult(json.dumps({"code": 0})).as_json()
        if EVENT_8 in script or '"event": 8' in script:
            self.calls.append((script, timeout))
            existed = self.muted
            self.muted = False
            # 麦本就开时 unlink ENOENT ⇒ code:-1（幂等语义，不是失败）
            return CommandResult(json.dumps({"code": 0 if existed else -1})).as_json()
        if MIC_FILE in script:
            self.calls.append((script, timeout))
            if self.fail_reads > 0:
                self.fail_reads -= 1
                raise asyncio.TimeoutError("fake mic read timeout")
            return CommandResult("off" if self.muted else "on").as_json()
        return await super().run_shell(script, timeout)

    def event_calls(self) -> list[str]:
        return [s for s, _ in self.calls if "event_notify" in s]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        ref.GLOBAL_STATES.clear()
        self.device = MuteAwareDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        ref.set_kws(_FakeKWS())
        self.speaker = speaker_module.SpeakerManager()

    def tearDown(self) -> None:
        ref.GLOBAL_STATES.clear()


class MicGateSerializationTest(_Base):
    def test_concurrent_gates_are_serialized(self) -> None:
        """并发播报不得嵌套快照；后进入者必须等前者退出。"""

        self.device.muted = False
        order: list[str] = []

        async def first() -> None:
            async with self.speaker.mic_gate("A", max_seconds=5):
                order.append("A-enter")
                await asyncio.sleep(0.05)
                order.append("A-exit")

        async def second() -> None:
            await asyncio.sleep(0.01)
            async with self.speaker.mic_gate("B", max_seconds=5):
                order.append("B-enter")

        async def scenario() -> None:
            await asyncio.gather(first(), second())

        _run(scenario())
        self.assertEqual(order, ["A-enter", "A-exit", "B-enter"])
        self.assertEqual(_run(self.speaker.get_mic()), "on")


class MicGateSnapshotTest(_Base):
    def test_snapshot_on_mutes_during_playback_and_restores_on(self) -> None:
        self.device.muted = False

        async def _scenario():
            async with self.speaker.mic_gate("test playback", max_seconds=5):
                return await self.speaker.get_mic()

        self.assertEqual(_run(_scenario()), "off", "播报期间必须是设备级闭麦")
        self.assertEqual(_run(self.speaker.get_mic()), "on", "播完恢复快照 on")
        events = self.device.event_calls()
        self.assertTrue(any(EVENT_7 in s for s in events), events)
        self.assertTrue(any(EVENT_8 in s for s in events), events)

    def test_snapshot_off_stays_off_and_never_unmutes(self) -> None:
        self.device.muted = True

        async def _scenario():
            async with self.speaker.mic_gate("test playback", max_seconds=5):
                return await self.speaker.get_mic()

        self.assertEqual(_run(_scenario()), "off")
        self.assertEqual(_run(self.speaker.get_mic()), "off", "不得把用户静音打开")
        self.assertFalse(any(EVENT_8 in s for s in self.device.event_calls()))

    def test_exception_inside_gate_still_restores_snapshot(self) -> None:
        self.device.muted = False

        async def _scenario():
            async with self.speaker.mic_gate("test playback", max_seconds=5):
                raise RuntimeError("playback boom")

        with self.assertRaises(RuntimeError):
            _run(_scenario())
        self.assertEqual(_run(self.speaker.get_mic()), "on")


class MicGateFallbackTest(_Base):
    def test_snapshot_read_failure_unmutes_when_kws_listening(self) -> None:
        self.device.muted = False
        self.device.fail_reads = 1  # 仅进入时的快照读取失败

        async def _scenario():
            async with self.speaker.mic_gate("test playback", max_seconds=5):
                pass

        _run(_scenario())
        self.assertEqual(_run(self.speaker.get_mic()), "on")
        self.assertTrue(any(EVENT_8 in s for s in self.device.event_calls()))

    def test_snapshot_read_failure_keeps_off_when_listening_disabled(self) -> None:
        ref.set_kws(_FakeKWS(disabled=True))
        self.device.muted = False
        self.device.fail_reads = 1

        async def _scenario():
            async with self.speaker.mic_gate("test playback", max_seconds=5):
                pass

        _run(_scenario())
        self.assertEqual(_run(self.speaker.get_mic()), "off", "停止聆听时保持 off")
        self.assertFalse(any(EVENT_8 in s for s in self.device.event_calls()))

    def test_mute_readback_failure_does_not_block_playback(self) -> None:
        self.device.muted = False
        self.device.fail_reads = 2  # 快照 + 闭麦回读都失败

        async def _scenario():
            async with self.speaker.mic_gate("test playback", max_seconds=5):
                return "played"

        self.assertEqual(_run(_scenario()), "played")
        self.assertEqual(_run(self.speaker.get_mic()), "on")


class MicGateWatchdogTest(_Base):
    def test_gate_over_max_seconds_forces_snapshot_restore(self) -> None:
        self.device.muted = False

        async def _scenario():
            async with self.speaker.mic_gate("leaked gate", max_seconds=0.05):
                await asyncio.sleep(0.2)
                return await self.speaker.get_mic()

        self.assertEqual(_run(_scenario()), "on", "超上限必须强制恢复快照")
        self.assertEqual(_run(self.speaker.get_mic()), "on")

    def test_gate_over_max_seconds_keeps_off_snapshot(self) -> None:
        self.device.muted = True

        async def _scenario():
            async with self.speaker.mic_gate("leaked gate", max_seconds=0.05):
                await asyncio.sleep(0.2)

        _run(_scenario())
        self.assertEqual(_run(self.speaker.get_mic()), "off")
        self.assertFalse(any(EVENT_8 in s for s in self.device.event_calls()))


class ReconcileTest(_Base):
    def test_stale_mute_is_cleared_on_startup(self) -> None:
        self.device.muted = True

        self.assertTrue(_run(self.speaker.reconcile_mic_state()))
        self.assertEqual(_run(self.speaker.get_mic()), "on")

    def test_listening_disabled_keeps_stale_mute(self) -> None:
        ref.set_kws(_FakeKWS(disabled=True))
        self.device.muted = True

        self.assertFalse(_run(self.speaker.reconcile_mic_state()))
        self.assertEqual(_run(self.speaker.get_mic()), "off")
        self.assertFalse(any(EVENT_8 in s for s in self.device.event_calls()))

    def test_healthy_mic_is_left_alone(self) -> None:
        self.device.muted = False

        self.assertFalse(_run(self.speaker.reconcile_mic_state()))
        self.assertEqual(self.device.event_calls(), [])


class SetMicSemanticsRegressionTest(_Base):
    def test_set_mic_on_sends_event_8(self) -> None:
        self.device.muted = True

        self.assertTrue(_run(self.speaker.set_mic(on=True)))
        self.assertIn(EVENT_8, self.device.event_calls()[0])
        self.assertFalse(self.device.muted)

    def test_set_mic_off_sends_event_7(self) -> None:
        self.assertTrue(_run(self.speaker.set_mic(on=False)))
        self.assertIn(EVENT_7, self.device.event_calls()[0])
        self.assertTrue(self.device.muted)

    def test_get_mic_reflects_mute_file(self) -> None:
        self.assertEqual(_run(self.speaker.get_mic()), "on")
        self.device.muted = True
        self.assertEqual(_run(self.speaker.get_mic()), "off")


if __name__ == "__main__":
    unittest.main(verbosity=2)
