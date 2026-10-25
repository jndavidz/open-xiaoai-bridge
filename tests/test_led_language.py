"""LED 灯语（A-6/P3 票 02）契约测试——纯内存（FakeDevice 边界）。

覆盖：

  1. 能力表登记契约（白名单/判据/互抢占纪律 notes）与 client 参数校验
     （index/rgb/effect 白名单拒绝）；
  2. pipeline 状态订阅面：注册/注销/多订阅者/异常隔离；
  3. 四态映射：listening=蓝、processing=琥珀、speaking=不干预（不与系统
     播放动画抢占）、idle=写黑归位（没点过灯不碰）；打断=红闪、故障=红定格；
  4. 串行纪律：灯写全部经串行锁（同一时刻一条流）；开关关闭零输出。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
for _path in (TESTS_DIR, ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fake_device import FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import device as device_pkg, ref  # noqa: E402
from core.device import registry  # noqa: E402
from core.services import led_language as led_module  # noqa: E402
from core.utils import pipeline as pipeline_module  # noqa: E402
from core.utils.pipeline import PipelineState  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        ref.GLOBAL_STATES.clear()
        device_pkg.reset_device()
        pipeline_module.reset_pipeline()
        led_module.reset_led_language()
        self.device = FakeDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        ref.set_speaker(speaker_module.SpeakerManager())
        self.addCleanup(device_pkg.reset_device)
        self.addCleanup(pipeline_module.reset_pipeline)
        self.addCleanup(led_module.reset_led_language)
        self.addCleanup(ref.GLOBAL_STATES.clear)


class LedCapabilityTest(_Base):
    def test_led_rows_registered_with_contract(self) -> None:
        for name in ("led_set_pixel", "led_effect", "led_clear_effect", "led_status"):
            spec = registry.resolve_capability(name)
            self.assertTrue(spec.notes or name in ("led_clear_effect",), name)

    def test_set_pixel_validates_index_and_rgb(self) -> None:
        caps = device_pkg.get_device()
        with self.assertRaises(ValueError):
            _run(caps.led_set_pixel(18, "ff0000"))
        with self.assertRaises(ValueError):
            _run(caps.led_set_pixel(0, "xyz"))
        with self.assertRaises(ValueError):
            _run(caps.led_set_pixel(0, "ff00"))
        self.device.on(contains="led_rgb", stdout="")
        res = _run(caps.led_set_pixel(3, "0xFF00AA"))
        self.assertTrue(res.ok)
        script = self.device.matching_calls("led_rgb")[0]
        self.assertIn("3 0xff00aa", script)
        self.assertIn("/sys/devices/i2c-0/0-003a/led_rgb", script)

    def test_effect_whitelist_rejects_8_and_above_17(self) -> None:
        caps = device_pkg.get_device()
        for bad in (8, 17, 42, -1):
            with self.assertRaises(ValueError, msg=f"L={bad}"):
                _run(caps.led_effect(bad))
        self.device.on(contains="led show", stdout='{"code":0}')
        self.assertTrue(_run(caps.led_effect(3)).ok)


class PipelineStateSubscriptionTest(_Base):
    def test_subscribe_unsubscribe_and_payload(self) -> None:
        events: list[tuple] = []
        unsub = pipeline_module.subscribe_state(
            lambda event, data: events.append((event, dict(data)))
        )
        pipeline_module.begin_turn("local_asr")
        pipeline_module.set_state(PipelineState.PROCESSING)
        pipeline_module.end_turn("cancelled:stream_segment")
        unsub()
        pipeline_module.set_state(PipelineState.IDLE)  # 注销后不再收
        kinds = [(e, d.get("state") or d.get("outcome")) for e, d in events]
        self.assertIn(("state_changed", "listening"), kinds)
        self.assertIn(("state_changed", "processing"), kinds)
        self.assertIn(("turn_ended", "cancelled:stream_segment"), kinds)
        self.assertEqual(3, len(events))

    def test_subscriber_exception_is_isolated(self) -> None:
        good: list[str] = []

        def _bad(event, data):
            raise RuntimeError("boom")

        unsub_bad = pipeline_module.subscribe_state(_bad)
        unsub_good = pipeline_module.subscribe_state(
            lambda event, data: good.append(event)
        )
        pipeline_module.set_state(PipelineState.PROCESSING)
        unsub_bad()
        unsub_good()
        self.assertEqual(["state_changed"], good)


class LedLanguageRenderingTest(_Base):
    def _attach(self, enabled: bool = True) -> led_module.LedLanguage:
        led = led_module.get_led_language()
        led.config = mock.Mock()
        led.config.get_app_config = lambda key, default=None: enabled
        led.attach(asyncio.new_event_loop())
        return led

    def test_four_state_mapping(self) -> None:
        self.device.on(contains="led_rgb", stdout="")
        led = self._attach(enabled=True)

        async def scenario():
            await led._render_state("listening")
            calls_blue = [c for c in self.device.calls if "2e6fdb" in c[0]]
            self.assertEqual(led_module.PIXEL_COUNT, len(calls_blue))
            await led._render_state("processing")
            calls_amber = [c for c in self.device.calls if "f0a902" in c[0]]
            self.assertEqual(led_module.PIXEL_COUNT, len(calls_amber))
            # speaking：不干预（系统 L=3/L=12 播放动画自带）
            before = len(self.device.calls)
            await led._render_state("speaking")
            self.assertEqual(before, len(self.device.calls))
            # idle：写黑归位（18 灯 0x000000）
            await led._render_state("idle")
            calls_off = [c for c in self.device.calls if "000000" in c[0]]
            self.assertEqual(led_module.PIXEL_COUNT, len(calls_off))

        _run(scenario())

    def test_idle_without_held_color_touches_nothing(self) -> None:
        self.device.on(contains="led_rgb", stdout="")
        led = self._attach(enabled=True)

        async def scenario():
            await led._render_state("idle")  # 从未点灯
            self.assertEqual(0, len(self.device.calls))

        _run(scenario())

    def test_interrupt_flash_and_error_hold(self) -> None:
        self.device.on(contains="led_rgb", stdout="")
        led = self._attach(enabled=True)

        async def scenario():
            with mock.patch.object(led_module.asyncio, "sleep", new=mock.AsyncMock()):
                await led._render("turn_ended", {"outcome": "cancelled:wakeup_interrupt"})
                red = [c for c in self.device.calls if "f85869" in c[0]]
                off = [c for c in self.device.calls if "000000" in c[0]]
                self.assertEqual(led_module.PIXEL_COUNT, len(red))
                self.assertEqual(led_module.PIXEL_COUNT, len(off))
                await led._render("turn_ended", {"outcome": "error"})
                red2 = [c for c in self.device.calls if "f85869" in c[0]]
                self.assertEqual(led_module.PIXEL_COUNT * 2, len(red2))

        _run(scenario())

    def test_disabled_switch_emits_nothing(self) -> None:
        self.device.on(contains="led_rgb", stdout="")
        led = self._attach(enabled=False)
        # 事件回调直接短路（enabled=False）
        self.assertIsNone(led._on_pipeline_event("state_changed", {"state": "listening"}))
        self.assertEqual(0, len(self.device.calls))


if __name__ == "__main__":
    unittest.main()
