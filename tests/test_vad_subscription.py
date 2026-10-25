"""VAD 订阅 seam（P2 ws ticket 04）契约测试——纯内存。

覆盖（票面三条验收）：

  1. 订阅接口成形：注册/注销/多消费者/线程上抛模型明确（回调在检测
     线程同步上抛、订阅者异常被隔离记 WARNING、注销幂等）；
  2. 现有行为不变：事件广播语义与旧 VAD→EventManager 直连等价
     （默认消费者转发）；帧 tap 在 VAD 状态处理前上抛（与旧猴补丁
     的 wrap 语义 1:1）；对话控制器不再替换 VAD/EventManager 内部面；
  3. 多消费者并发：两个 tap 同时收帧、互不干扰、注销即停。

真 _VAD 类的实例化经 silero stub（onnx 模型不加载）；检测线程不启动，
帧处理由用例直接驱动 _handle_*_frame（与检测循环的调用点相同）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# onnxruntime / 模型文件 stub（先于 core.services.audio.vad 导入就位）
if "core.services.audio.vad.silero" not in sys.modules:
    sys.modules["core.services.audio.vad.silero"] = types.SimpleNamespace(
        Silero=types.SimpleNamespace(vad=lambda frames, sr: 0.0)
    )
if "open_xiaoai_server" not in sys.modules:
    sys.modules["open_xiaoai_server"] = types.ModuleType("open_xiaoai_server")
sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))

from core import ref  # noqa: E402
from core.services.audio.vad import _VAD  # noqa: E402
from core.utils.logger import logger as app_logger  # noqa: E402
from core.wakeup_session import EventManager  # noqa: E402


class _StreamStub:
    """pause/resume 触碰的流面（stop_stream/clear_input/start_stream）。"""

    def stop_stream(self):
        pass

    def start_stream(self):
        pass

    def clear_input(self):
        pass


def _make_vad() -> _VAD:
    ref.GLOBAL_STATES.clear()
    vad = _VAD()
    vad.stream = _StreamStub()
    return vad


class VadSubscriptionTest(unittest.TestCase):
    def setUp(self):
        self.vad = _make_vad()
        self.addCleanup(ref.GLOBAL_STATES.clear)

    # ---- 1. 订阅接口成形 ----

    def test_subscribe_unsubscribe_idempotent(self):
        received: list[tuple[str, bytes]] = []
        unsubscribe = self.vad.subscribe_frames(
            lambda kind, frames: received.append((kind, frames))
        )
        self.vad._handle_speech_frame(b"\x01\x02")
        unsubscribe()
        unsubscribe()  # 幂等：重复注销不抛
        self.vad._handle_speech_frame(b"\x03\x04")
        self.assertEqual(1, len(received))

    def test_subscriber_exception_is_isolated(self):
        """订阅者抛异常：记 WARNING、不传染其他消费者、不中断广播。"""
        good: list[tuple[str, bytes]] = []

        def _bad(kind, frames):
            raise RuntimeError("tap boom")

        unsub_bad = self.vad.subscribe_frames(_bad)
        unsub_good = self.vad.subscribe_frames(
            lambda kind, frames: good.append((kind, frames))
        )
        with mock.patch.object(app_logger, "warning") as warning:
            self.vad._handle_silence_frame(b"\x05\x06")
        self.assertEqual([("silence", b"\x05\x06")], good)
        self.assertTrue(
            any("tap boom" in str(call.args[0]) for call in warning.call_args_list),
            [str(call.args[0]) for call in warning.call_args_list],
        )
        unsub_bad()
        unsub_good()

    # ---- 2. 现有行为不变 ----

    def test_default_consumer_forwards_to_event_manager(self):
        """默认事件消费者：广播 → EventManager.on_speech/on_silence（旧调用面
        语义保持，对 EventManager 回调点的临时替换继续有效）。"""
        self.vad.target = "speech"
        with mock.patch.object(
            EventManager, "on_speech", create=True
        ) as on_speech:
            self.vad._handle_speech_frame(b"\x00" * 8192)  # 越过 250ms 阈值
        self.assertEqual(1, on_speech.call_count)
        payload = on_speech.call_args.args[0]
        self.assertIsInstance(payload, bytes)
        self.assertGreaterEqual(len(payload), 8192)

    def test_speech_event_carries_buffered_frames(self):
        events: list[tuple[str, object]] = []
        unsub = self.vad.subscribe_events(lambda event, payload: events.append((event, payload)))
        self.vad.target = "speech"
        for _ in range(8):
            self.vad._handle_speech_frame(b"\x00" * 1024)
        # 8 帧 = 8192 bytes > 8000 bytes（250ms）⇒ 广播 speech（payload =
        # speech_frames 全量，含静音前导）
        self.assertEqual(1, len(events))
        self.assertEqual("speech", events[0][0])
        self.assertEqual(8192, len(events[0][1]))
        unsub()

    def test_silence_event_fires_on_silence_target(self):
        events: list[tuple[str, object]] = []
        unsub = self.vad.subscribe_events(lambda event, payload: events.append((event, payload)))
        self.vad.target = "silence"
        for _ in range(8):
            self.vad._handle_silence_frame(b"\x00" * 1024)
        for _ in range(16):
            self.vad._handle_silence_frame(b"\x00" * 1024)
        self.assertEqual([("silence", None)], events)
        unsub()

    def test_frame_tap_fires_before_state_handling(self):
        """帧 tap 在 VAD 状态处理前上抛（与旧猴补丁 wrap 语义 1:1）：
        tap 看到的帧包括触发事件广播的那一帧。"""
        seen: list[tuple[str, bytes]] = []
        unsub = self.vad.subscribe_frames(
            lambda kind, frames: seen.append((kind, bytes(frames)))
        )
        self.vad.target = "speech"
        self.vad._handle_speech_frame(b"\x00" * 8192)
        unsub()
        self.assertEqual(1, len(seen))
        self.assertEqual("speech", seen[0][0])
        self.assertEqual(8192, len(seen[0][1]))

    # ---- 3. 多消费者并发 ----

    def test_two_taps_receive_same_frames_independently(self):
        first: list[bytes] = []
        second: list[bytes] = []
        unsub_first = self.vad.subscribe_frames(
            lambda kind, frames: first.append(bytes(frames))
        )
        unsub_second = self.vad.subscribe_frames(
            lambda kind, frames: second.append(bytes(frames))
        )
        for i in range(3):
            self.vad._handle_speech_frame(b"\x01" * 4)
            self.vad._handle_silence_frame(b"\x02" * 4)
        unsub_first()
        # 注销第一个 tap：第二个照常收帧（互不干扰）
        self.vad._handle_speech_frame(b"\x03" * 4)
        self.assertEqual(6, len(first))
        self.assertEqual(7, len(second))
        self.assertEqual(first, second[:6])
        unsub_second()


class ControllerSeamUsageTest(unittest.IsolatedAsyncioTestCase):
    """对话控制器经公开 seam 消费 VAD（不再猴补丁内部面）。"""

    def setUp(self):
        ref.GLOBAL_STATES.clear()
        self.vad = _make_vad()
        ref.set_vad(self.vad)
        self.addCleanup(ref.GLOBAL_STATES.clear)

    async def test_wait_for_speech_leaves_vad_internals_untouched(self):
        from core.external_conversation import ExternalConversationController

        class TestBackend:
            _rule_prompt = ""

        class TestController(ExternalConversationController):
            MANAGER = TestBackend
            CONFIG_PREFIX = "test"
            BACKEND_NAME = "Test"
            LOG_MODULE = "Test Conv"
            WAKEUP_SOURCE = "test"

        controller = TestController()
        controller._loop = asyncio.get_running_loop()

        speech_task = asyncio.ensure_future(controller._wait_for_speech(self.vad))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 等待期间：订阅在表上、VAD 内部处理器与 EventManager 回调槽未被替换
        self.assertEqual(2, len(self.vad._event_subscribers))  # 默认消费者 + 控制器
        self.assertEqual(1, len(self.vad._frame_subscribers))
        self.assertNotIn("_handle_speech_frame", self.vad.__dict__)
        self.assertNotIn("_handle_silence_frame", self.vad.__dict__)
        self.assertNotIn("on_speech", EventManager.__dict__)
        self.assertNotIn("on_silence", EventManager.__dict__)

        # 说 → 说完：经订阅 seam 驱动（模拟 VAD 检测线程回调）
        self.vad.target = "speech"
        controller_subscriber = self.vad._event_subscribers[-1]
        controller_subscriber("speech", b"\x01\x02")
        for kind, frames in (("speech", b"\x03\x04"), ("silence", b"\x05\x06")):
            for callback in list(self.vad._frame_subscribers):
                callback(kind, frames)
        controller_subscriber("silence", None)

        result = await asyncio.wait_for(speech_task, timeout=1)
        # 录音 = speech 事件携带的初始 buffer + is_recording 期间的两帧
        self.assertEqual(b"\x01\x02" + b"\x03\x04" + b"\x05\x06", result)
        # 结束后：订阅注销干净
        self.assertEqual(1, len(self.vad._event_subscribers))  # 仅剩默认消费者
        self.assertEqual(0, len(self.vad._frame_subscribers))

    async def test_wait_for_silence_leaves_event_manager_untouched(self):
        from core.external_conversation import ExternalConversationController

        class TestBackend:
            _rule_prompt = ""

        class TestController(ExternalConversationController):
            MANAGER = TestBackend
            CONFIG_PREFIX = "test"
            BACKEND_NAME = "Test"
            LOG_MODULE = "Test Conv"
            WAKEUP_SOURCE = "test"

        controller = TestController()
        controller._loop = asyncio.get_running_loop()

        silence_task = asyncio.ensure_future(controller._wait_for_silence(self.vad))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(2, len(self.vad._event_subscribers))  # 默认 + 控制器
        self.assertNotIn("on_silence", EventManager.__dict__)

        controller_subscriber = self.vad._event_subscribers[-1]
        controller_subscriber("silence", None)
        await asyncio.wait_for(silence_task, timeout=1)
        self.assertEqual(1, len(self.vad._event_subscribers))  # 注销干净


if __name__ == "__main__":
    unittest.main()
