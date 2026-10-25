"""句段 → TTS 流式 → 播放贯通（P1 ticket 04）契约测试——纯内存。

覆盖（spec《P1 流式对话管线》Implementation Decisions「TTS 流式」+ 票面 04）：

  1. 逐段起播：首段到达即断录起播（不等全部生成）、后续段接力播放、
     播放令牌整轮一个；
  2. 流水线性：第一段播放开始早于第二段产出（边生成边播）；
  3. 生长期打断：段间取消 ⇒ 剩余段不播、播放令牌定向杀流、静默退出
     （无 ERROR、无「打断后冒半句」）；
  4. 非流式退化（单句序列）：单段一次播放，与改前行为一致（回退路径）；
  5. 零段：不 begin 播放会话、不断录，上层走「没有收到回复」播报；
  6. 段播放异常：fallback 播该段后继续后续段；
  7. pump 异常：已播段保留，对话循环不挂（吞错记日志）；
  8. note_tts_chars 累加口径（分段播放每段各记一次）。
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

# ---- Rust 扩展 stub（先于 core.* 导入就位；沿用 discover 序里既有的桩）----

_NATIVE = sys.modules.get("open_xiaoai_server")
if _NATIVE is None:
    _NATIVE = types.ModuleType("open_xiaoai_server")
    sys.modules["open_xiaoai_server"] = _NATIVE

if not hasattr(_NATIVE, "begin_playback_session"):
    _NATIVE.begin_playback_session_calls = []
    _NATIVE.stop_tts_playback_calls = []

    def _begin_playback_session() -> int:
        _NATIVE.begin_playback_session_calls.append(True)
        return 7

    def _stop_tts_playback(token: int) -> None:
        _NATIVE.stop_tts_playback_calls.append(token)

    _NATIVE.begin_playback_session = _begin_playback_session
    _NATIVE.stop_tts_playback = _stop_tts_playback

if not hasattr(_NATIVE, "tts_play"):
    _NATIVE.tts_play = mock.AsyncMock(return_value=None)
if not hasattr(_NATIVE, "tts_stream_play"):
    _NATIVE.tts_stream_play = mock.AsyncMock(return_value=None)

sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))

from core import ref  # noqa: E402
from core.external_conversation import ExternalConversationController  # noqa: E402
from core.utils import pipeline as pipeline_module  # noqa: E402
from core.utils import turn_trace  # noqa: E402
from core.utils.logger import logger as app_logger  # noqa: E402
from core.utils.pipeline import TurnCancelledError  # noqa: E402
from core.utils.turn_trace import TurnTrace  # noqa: E402
from core.wakeup_session import EventManager  # noqa: E402

import open_xiaoai_server as native_stub  # noqa: E402


# ---- fakes ----


class FakeSpeaker:
    def __init__(self):
        self.calls: list[tuple] = []

    async def play(self, text=None, url=None, buffer=None, blocking=True, timeout=None):
        self.calls.append(("play", text, url, buffer))
        return True

    async def wake_up(self, awake=True, silent=False):
        self.calls.append(("wake_up", awake, silent))

    async def stop_device_audio(self):
        self.calls.append(("stop_device_audio",))


class SegmentBackend:
    """流式后端 fake：segments 逐段 yield，播放调用与全局事件序都记账。"""

    _rule_prompt = ""

    def __init__(self, segments: list[str]):
        self.segments = list(segments)
        self.sent: list[str] = []
        self.played: list[tuple[str, int | None]] = []
        self.events: list[tuple] = []
        self.aclosed = False
        self.fail_on_segment: str | None = None
        self.cancel_after_segment: str | None = None
        self.cancel_hook = None

    async def stream_response_segments(self, text: str):
        self.sent.append(text)
        try:
            for segment in self.segments:
                if segment is _DELAY:
                    await asyncio.sleep(0.05)
                    continue
                self.events.append(("yield", segment))
                yield segment
        finally:
            self.aclosed = True
            self.events.append(("aclose", None))

    def get_tts_speaker_for_session_key(self, session_key=None):
        return None

    async def _play_response_with_tts(self, text, tts_speaker=None, playback_token=None):
        if self.fail_on_segment == text:
            raise RuntimeError(f"tts boom: {text}")
        self.played.append((text, playback_token))
        self.events.append(("play", text))
        if self.cancel_after_segment == text and self.cancel_hook:
            hook, self.cancel_hook = self.cancel_hook, None
            hook()


# 队列里的「延迟标记」：pump 在此让出控制权 50ms（模拟 LLM 还在生成）。
_DELAY = object()


def _make_controller(backend) -> ExternalConversationController:
    class TestController(ExternalConversationController):
        MANAGER = backend
        CONFIG_PREFIX = "test"
        BACKEND_NAME = "Test"
        LOG_MODULE = "Test Conv"
        WAKEUP_SOURCE = "test"

    return TestController()


class SpeakSegmentsTestBase(unittest.IsolatedAsyncioTestCase):
    """单元级基类：直接驱动 _speak_response_segments，mock 断录/开录面。"""

    def setUp(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()
        native_stub.begin_playback_session_calls.clear()
        native_stub.stop_tts_playback_calls.clear()
        self.speaker = FakeSpeaker()
        ref.GLOBAL_STATES.clear()
        ref.set_speaker(self.speaker)
        self._patches = [
            mock.patch("core.ref.get_speaker", return_value=self.speaker),
        ]
        from core.xiaoai import XiaoAI

        self.recording_events: list[str] = []
        self._patches.append(
            mock.patch.object(
                XiaoAI,
                "stop_recording",
                new=mock.AsyncMock(
                    side_effect=lambda: self.recording_events.append("stop")
                ),
            )
        )
        self._patches.append(
            mock.patch.object(
                XiaoAI,
                "start_recording",
                new=mock.AsyncMock(
                    side_effect=lambda: self.recording_events.append("start")
                ),
            )
        )
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(ref.GLOBAL_STATES.clear)
        self.addCleanup(pipeline_module.reset_pipeline)
        self.addCleanup(turn_trace.reset_turn_trace)

    def _make_backend(self, segments, **kwargs) -> SegmentBackend:
        backend = SegmentBackend(segments, **kwargs)
        self.controller = _make_controller(backend)
        self.controller._loop = asyncio.get_running_loop()
        # 检查点依赖当前轮：无轮时直通（生产里 _conversation_loop 持有）
        self.controller._pipeline_turn = pipeline_module.begin_turn("test")
        return backend


class PerSegmentPlaybackTest(SpeakSegmentsTestBase):
    async def test_first_segment_plays_before_rest_generated(self):
        """首段到达即断录起播；第二段仍在生成时第一段已开播（流水线性）。"""
        backend = self._make_backend(["第一句话了。", _DELAY, "第二句话了。"])
        controller = self.controller
        send_sound_events: list[str] = []

        async def _fake_send_sound():
            send_sound_events.append("send_sound")

        with mock.patch.object(controller, "_play_send_sound", _fake_send_sound):
            played = await controller._speak_response_segments(
                "讲个故事", with_send_sound=True
            )

        self.assertEqual(["第一句话了。", "第二句话了。"], played)
        # 每段独立播放、同一令牌；播放会话整轮只 begin 一次
        self.assertEqual(
            [("第一句话了。", 7), ("第二句话了。", 7)], backend.played
        )
        self.assertEqual([True], native_stub.begin_playback_session_calls)
        # 提示音在等首段期间播放（先于任何段播放）
        self.assertEqual(["send_sound"], send_sound_events)
        # 事件序：断录 < 首段播放 < 第二段产出（边生成边播的判据）
        self.assertIn("stop", self.recording_events)
        self.assertLess(
            backend.events.index(("play", "第一句话了。")),
            backend.events.index(("yield", "第二句话了。")),
            backend.events,
        )
        self.assertLess(
            self.recording_events.index("stop"),
            backend.events.index(("play", "第一句话了。")),
        )
        # 播放结束令牌归位
        self.assertIsNone(controller._playback_token)

    async def test_non_streaming_single_segment_equivalent(self):
        """非流式退化（单句序列）：单段一次播放，不断录时机不变（回退路径）。"""
        backend = self._make_backend(["整段回复。"])
        played = await self.controller._speak_response_segments(
            "你好", with_send_sound=False
        )
        self.assertEqual(["整段回复。"], played)
        self.assertEqual([("整段回复。", 7)], backend.played)
        self.assertEqual([True], native_stub.begin_playback_session_calls)

    async def test_zero_segments_returns_empty_without_session(self):
        """零段：不起播、不建播放会话、不断录（上层走「没有收到回复」）。"""
        backend = self._make_backend([])
        played = await self.controller._speak_response_segments(
            "你好", with_send_sound=False
        )
        self.assertEqual([], played)
        self.assertEqual([], backend.played)
        self.assertEqual([], native_stub.begin_playback_session_calls)
        self.assertEqual([], self.recording_events)

    async def test_segment_failure_falls_back_and_continues(self):
        """段播放异常：该段经 speaker fallback，后续段照播。"""
        backend = self._make_backend(["第一句话了。", "第二句话了。"])
        backend.fail_on_segment = "第一句话了。"
        played = await self.controller._speak_response_segments(
            "你好", with_send_sound=False
        )
        self.assertEqual(["第一句话了。", "第二句话了。"], played)
        self.assertEqual([("第二句话了。", 7)], backend.played)
        self.assertIn(("play", "第一句话了。", None, None), self.speaker.calls)

    async def test_pump_failure_keeps_played_segments(self):
        """pump 异常：已播段保留、吞错记日志（流中断按已有内容收尾）。"""
        backend = self._make_backend(["第一句话了。"])

        async def _boom(text: str):
            yield "第一句话了。"
            raise RuntimeError("llm boom")

        backend.stream_response_segments = _boom
        with mock.patch.object(app_logger, "error") as error:
            played = await self.controller._speak_response_segments(
                "你好", with_send_sound=False
            )
        self.assertEqual(["第一句话了。"], played)
        self.assertTrue(
            any("Segment pump failed" in str(call.args[0]) for call in error.call_args_list),
            [str(call.args[0]) for call in error.call_args_list],
        )


class StreamInterruptTest(SpeakSegmentsTestBase):
    async def test_cancel_between_segments_stops_remaining(self):
        """生长期打断：段间取消 ⇒ 剩余段不播、无新播放会话、静默退出。"""
        backend = self._make_backend(["第一句话了。", "第二句话了。", "第三句话了。"])

        def _interrupt():
            pipeline_module.cancel_current_turn("wakeup_interrupt")
            native_stub.stop_tts_playback(7)  # 打断第二层：播放令牌定向杀流

        backend.cancel_after_segment = "第二句话了。"
        backend.cancel_hook = _interrupt
        with mock.patch.object(app_logger, "error") as error:
            with self.assertRaises(TurnCancelledError):
                await self.controller._speak_response_segments(
                    "你好", with_send_sound=False
                )
        # 已播两段，第三段被检查点拦下
        self.assertEqual(
            [("第一句话了。", 7), ("第二句话了。", 7)], backend.played
        )
        # 播放会话只建过一次；令牌被定向停止过
        self.assertEqual([True], native_stub.begin_playback_session_calls)
        self.assertEqual([7], native_stub.stop_tts_playback_calls)
        # pump 被 cancel：generator aclose 已执行（上游 SSE 不泄漏）
        self.assertTrue(backend.aclosed)
        self.assertIsNone(self.controller._playback_token)
        error.assert_not_called()


class TtsCharsAccumulateTest(unittest.TestCase):
    def test_note_tts_chars_accumulates_across_segments(self):
        """note_tts_chars 累加口径（ticket 04）：分段播放每段各记一次。"""
        trace = TurnTrace(1, source="local_asr", asr_chars=4)
        trace.note_tts_chars(5)
        trace.note_tts_chars(7)
        summary = trace.summary()
        self.assertEqual(12, summary["tts_chars"])

    def test_note_tts_chars_first_call_matches_legacy(self):
        """单次调用与旧覆盖口径等价（回退路径行为不变）。"""
        trace = TurnTrace(1, source="local_asr", asr_chars=4)
        trace.note_tts_chars(44)
        self.assertEqual(44, trace.summary()["tts_chars"])

    def test_note_tts_chars_ignores_invalid(self):
        trace = TurnTrace(1, source="local_asr", asr_chars=4)
        trace.note_tts_chars(44)
        trace.note_tts_chars(-1)
        trace.note_tts_chars(True)
        trace.note_tts_chars("bad")
        self.assertEqual(44, trace.summary()["tts_chars"])


# ---- 对话循环级：打断经完整链路静默落地 ----


class FakeVad:
    def __init__(self):
        self.modes: list[tuple[str, str]] = []
        self.paused = 0

    def resume(self, mode: str):
        self.modes.append(("resume", mode))

    def pause(self):
        self.paused += 1


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class LoopLevelInterruptTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()
        native_stub.begin_playback_session_calls.clear()
        native_stub.stop_tts_playback_calls.clear()
        self.vad = FakeVad()
        self.speaker = FakeSpeaker()
        ref.GLOBAL_STATES.clear()
        ref.set_vad(self.vad)
        ref.set_speaker(self.speaker)
        from core.xiaoai import XiaoAI

        self._patches = [
            mock.patch("core.ref.get_vad", return_value=self.vad),
            mock.patch("core.ref.get_speaker", return_value=self.speaker),
            mock.patch.object(XiaoAI, "start_recording", mock.AsyncMock()),
            mock.patch.object(XiaoAI, "stop_recording", mock.AsyncMock()),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(ref.GLOBAL_STATES.clear)
        self.addCleanup(pipeline_module.reset_pipeline)
        self.addCleanup(turn_trace.reset_turn_trace)

    async def test_streaming_interrupt_in_generation_silently_exits(self):
        """对话循环级：流式轮在段播放后被打断 ⇒ 静默退出、无残留段、无 ERROR。

        取消注入在第二段播放完成时（模拟播放期喊唤醒词：置取消 + 令牌杀流），
        第三段已被 pump 产出、正排在队列里——检查点必须把它拦下。
        """
        backend = SegmentBackend(
            ["第一句话了。", "第二句话了。", "第三句话了。"]
        )

        def _interrupt():
            pipeline_module.cancel_current_turn("wakeup_interrupt")
            native_stub.stop_tts_playback(7)

        backend.cancel_after_segment = "第二句话了。"
        backend.cancel_hook = _interrupt
        controller = _make_controller(backend)
        self.controller = controller

        async def _fake_wait_speech(vad):
            return b"\x01\x02"

        with mock.patch(
            "core.services.audio.asr.service.ASRService",
            **{"asr.return_value": "讲个故事"},
        ):
            with mock.patch.object(
                controller, "_wait_for_speech", _fake_wait_speech
            ):
                with mock.patch.object(
                    controller, "_wait_for_silence", mock.AsyncMock()
                ):
                    with mock.patch.object(app_logger, "error") as error:
                        await asyncio.wait_for(controller.start(), timeout=3)

        # 打断点在第二段播放完成后：第三段（已产出、在队列里）被
        # stream_segment 检查点拦下，不再起播
        self.assertEqual(
            [("第一句话了。", 7), ("第二句话了。", 7)], backend.played
        )
        self.assertEqual([True], native_stub.begin_playback_session_calls)
        self.assertEqual([7], native_stub.stop_tts_playback_calls)
        self.assertTrue(backend.aclosed)
        self.assertFalse(controller.active)
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
