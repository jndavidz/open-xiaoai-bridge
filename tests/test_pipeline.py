"""Pipeline 管线态 / 轮次标识 / 取消传播（P1 ticket 02）契约测试。

全部纯内存、stdlib unittest：fake backend / speaker / vad + stub Rust 扩展，
不连真机、不联网、不触 onnx / sherpa。

覆盖：

  1. 管线态迁移与日志（turn=N state=x->y；与设备协议态分层并存）；
  2. 轮次标识贯穿：pipeline 号 = [TurnTrace] turn=N（同源），含
     speech_end → asr_final 段（本地链路「说完→定稿」缺口）；
  3. 取消传播检查点（ASR 定稿后 / LLM 请求前 / 播放等待内）：触发取消 ⇒
     本轮静默退出（无 ERROR 日志、请求未发 / 未播、无残留播放——
     播放令牌定向杀流照常发生）；
  4. 打断两层机制：置取消（新增面）+ controller.stop / task.cancel /
     stop_device_audio（既有面，回归由 test_backend_registry 覆盖）；
  5. TTSRouter 埋点缺口：provider 错误入 trace（tts_ok=0/tts_fallback=1），
     Rust 通道成功收尾（rust_channel 口径，accept_to_start 留 -1）。
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

# ---- Rust 扩展 / 重依赖 stub（先于 core.* 导入就位）----
#
# 沿用 sys.modules 里既有的桩对象（load_speaker_module 可能已造好，别的
# 测试模块也持有它的引用），只补缺失的属性——绝不替换对象，否则
# speaker.py 等早先 import 的模块仍指旧对象，补属性会补到错的对象上。

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
    _NATIVE.tts_play = mock.AsyncMock(return_value=None)
    _NATIVE.tts_stream_play = mock.AsyncMock(return_value=None)

# ASR 服务模块顶层 import sherpa_onnx（方法内才用）；stub 掉以支持纯内存用例。
sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))

from core import ref  # noqa: E402
from core.external_conversation import ExternalConversationController  # noqa: E402
from core.utils import pipeline as pipeline_module  # noqa: E402
from core.utils import turn_trace  # noqa: E402
from core.utils.logger import logger as app_logger  # noqa: E402
from core.utils.pipeline import (  # noqa: E402
    CHECKPOINT_ASR_FINAL,
    CHECKPOINT_PLAYBACK,
    CHECKPOINT_PRE_LLM,
    PipelineState,
    TurnCancelledError,
)
from core.wakeup_session import EventManager  # noqa: E402

import open_xiaoai_server as native_stub  # noqa: E402


# ---- fakes ----


class FakeVad:
    """VAD 替身：实现对话控制器依赖的公开订阅 seam（P2 ws ticket 04 起
    _wait_for_speech / _wait_for_silence 走 subscribe_events/subscribe_frames，
    不再猴补丁内部处理器）。"""

    def __init__(self):
        self.modes: list[tuple[str, str]] = []
        self.paused = 0
        self.event_subscribers: list = []
        self.frame_subscribers: list = []

    def resume(self, mode: str):
        self.modes.append(("resume", mode))

    def pause(self):
        self.paused += 1

    def subscribe_events(self, callback):
        self.event_subscribers.append(callback)

        def _unsubscribe():
            try:
                self.event_subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def subscribe_frames(self, callback):
        self.frame_subscribers.append(callback)

        def _unsubscribe():
            try:
                self.frame_subscribers.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    def emit_event(self, event: str, payload=None):
        for callback in list(self.event_subscribers):
            callback(event, payload)


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


class FakeBackend:
    """ExternalConversationController 依赖面的最小后端 fake。

    _play_response_with_tts 模拟真实链路（backend_base → TTSRouter.play）的
    TurnTrace 行为：TTS 段埋点 + Rust 通道收尾，与生产口径一致。
    stream_response_segments 模拟基类默认「单句序列」退化（ticket 03）。
    """

    _rule_prompt = ""

    def __init__(self, response: str = "好的"):
        self.response = response
        self.sent: list[str] = []
        self.played: list[tuple[str, int | None]] = []
        # 注入点：对应三个检查点前后的人工打断时刻
        self.cancel_before_send = None
        self.cancel_during_play = None

    async def _send_and_track(self, text: str):
        if self.cancel_before_send:
            self.cancel_before_send()
        turn_trace.mark_llm_start()
        turn_trace.mark_llm_end()
        turn_trace.note_reply_chars(len(self.response))
        self.sent.append(text)
        return f"run-{len(self.sent)}"

    async def _wait_response(self, run_id: str):
        return self.response

    async def send(self, text: str, wait_response: bool = True):
        if self.cancel_before_send:
            self.cancel_before_send()
        turn_trace.mark_llm_start()
        turn_trace.mark_llm_end()
        turn_trace.note_reply_chars(len(self.response))
        self.sent.append(text)
        return self.response

    async def stream_response_segments(self, text: str):
        if self.cancel_before_send:
            self.cancel_before_send()
        response = await self.send(text, wait_response=True)
        if response:
            yield response

    def get_tts_speaker_for_session_key(self, session_key=None):
        return None

    async def _play_response_with_tts(self, text, tts_speaker=None, playback_token=None):
        if self.cancel_during_play:
            self.cancel_during_play()
        self.played.append((text, playback_token))
        turn_trace.mark_tts_start()
        turn_trace.mark_tts_end()
        turn_trace.note_tts_result(ok=True, fallback=False)
        turn_trace.note_playback(
            finished=True,
            started=False,
            reason="rust_channel",
            wait_started_at=None,
            started_at_ms=None,
            finished_at_ms=None,
        )


def _make_controller(backend: FakeBackend) -> ExternalConversationController:
    class TestController(ExternalConversationController):
        MANAGER = backend
        CONFIG_PREFIX = "test"
        BACKEND_NAME = "Test"
        LOG_MODULE = "Test Conv"
        WAKEUP_SOURCE = "test"

    return TestController()


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _speech_hook_installed(vad) -> bool:
    """_wait_for_speech 的语音订阅是否挂在 VAD 订阅表上（ticket 04 起
    经公开 seam，不再替换 EventManager 回调槽）。"""
    return len(vad.event_subscribers) > 0


# ---- 模块级单测 ----


class PipelineStateTest(unittest.TestCase):
    def setUp(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()

    def tearDown(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()

    def test_turn_lifecycle_logs_state_transitions(self):
        with mock.patch.object(pipeline_module.logger, "info") as info:
            turn = pipeline_module.begin_turn("local_asr")
            pipeline_module.set_state(PipelineState.PROCESSING)
            pipeline_module.set_state(PipelineState.SPEAKING)
            pipeline_module.end_turn("played")

        lines = [str(call.args[0]) for call in info.call_args_list]
        self.assertTrue(
            any("turn=1 state=idle->listening src=local_asr" in line for line in lines),
            lines,
        )
        self.assertTrue(
            any("turn=1 state=listening->processing" in line for line in lines), lines
        )
        self.assertTrue(
            any("turn=1 state=processing->speaking" in line for line in lines), lines
        )
        self.assertTrue(
            any("turn=1 state=speaking->idle outcome=played" in line for line in lines),
            lines,
        )
        self.assertEqual(PipelineState.IDLE, pipeline_module.state())
        self.assertEqual(1, turn.turn_id)

    def test_same_state_transition_is_silent(self):
        pipeline_module.begin_turn("local_asr")
        with mock.patch.object(pipeline_module.logger, "info") as info:
            pipeline_module.set_state(PipelineState.LISTENING)
        info.assert_not_called()

    def test_checkpoint_raises_when_cancelled(self):
        turn = pipeline_module.begin_turn("xiaoai_asr")
        turn.checkpoint(CHECKPOINT_PRE_LLM)  # 未取消：不抛
        self.assertTrue(turn.cancel("wakeup_interrupt"))
        self.assertFalse(turn.cancel("again"))  # 幂等
        with self.assertRaises(TurnCancelledError) as ctx:
            turn.checkpoint(CHECKPOINT_PRE_LLM)
        self.assertEqual(1, ctx.exception.turn_id)
        self.assertEqual(CHECKPOINT_PRE_LLM, ctx.exception.checkpoint)
        self.assertEqual("wakeup_interrupt", ctx.exception.reason)

    def test_new_turn_supersedes_previous(self):
        first = pipeline_module.begin_turn("local_asr")
        second = pipeline_module.begin_turn("local_asr")
        self.assertTrue(first.cancelled)
        self.assertEqual("superseded", first.cancel_reason)
        self.assertFalse(second.cancelled)
        with self.assertRaises(TurnCancelledError):
            first.checkpoint(CHECKPOINT_PLAYBACK)

    def test_cancel_helpers(self):
        self.assertEqual(0, pipeline_module.cancel_all_turns("wakeup_interrupt"))
        turn = pipeline_module.begin_turn("local_asr")
        self.assertTrue(pipeline_module.cancel_current_turn("wakeup_interrupt"))
        self.assertTrue(turn.cancelled)
        self.assertEqual(
            {
                "state": "listening",
                "turn": 1,
                "turn_source": "local_asr",
                "cancelled": True,
                "cancel_reason": "wakeup_interrupt",
            },
            pipeline_module.snapshot(),
        )
        pipeline_module.end_turn("stopped")
        self.assertEqual(
            {
                "state": "idle",
                "turn": None,
                "turn_source": "",
                "cancelled": False,
                "cancel_reason": "",
            },
            pipeline_module.snapshot(),
        )

    def test_trace_reuses_pipeline_turn_id(self):
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_ENABLED: "1"}):
            pipeline_module.begin_turn("local_asr")
            trace = turn_trace.begin_turn("local_asr", asr_chars=3, turn_id=2)
            self.assertIsNotNone(trace)
            self.assertEqual(2, trace.turn_id)
            # 缺省（非对话入口）自增，且不与传入号重叠（计数器随传入号推进）
            standalone = turn_trace.begin_turn("local_asr", asr_chars=1)
            self.assertEqual(3, standalone.turn_id)


# ---- 对话循环级：轮次贯穿 + 取消传播 ----


class ConversationLoopTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()
        native_stub.begin_playback_session_calls.clear()
        native_stub.stop_tts_playback_calls.clear()
        self.vad = FakeVad()
        self.speaker = FakeSpeaker()
        self.backend = FakeBackend()
        self.controller = _make_controller(self.backend)
        ref.GLOBAL_STATES.clear()
        ref.set_vad(self.vad)
        ref.set_speaker(self.speaker)
        from core.xiaoai import XiaoAI

        self._xiaoai = XiaoAI
        self._start_recording = mock.AsyncMock()
        self._stop_recording = mock.AsyncMock()
        self._patches = [
            mock.patch("core.ref.get_vad", return_value=self.vad),
            mock.patch("core.ref.get_speaker", return_value=self.speaker),
            mock.patch(
                "core.services.audio.asr.service.ASRService",
                **{"asr.return_value": "你好"},
            ),
            mock.patch.object(
                XiaoAI, "start_recording", self._start_recording
            ),
            mock.patch.object(
                XiaoAI, "stop_recording", self._stop_recording
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(ref.GLOBAL_STATES.clear)
        self.addCleanup(pipeline_module.reset_pipeline)
        self.addCleanup(turn_trace.reset_turn_trace)

    def _start_loop(self) -> asyncio.Task:
        task = asyncio.create_task(self.controller.start())
        self.addCleanup(lambda: task.cancel() if not task.done() else None)
        return task

    async def _drive_speech(self, speech_round: int = 1, utterance: bytes = b"\x01\x02"):
        """模拟第 speech_round 轮的完整语音（说 → 说完）。"""
        ok = await _wait_until(
            lambda: _speech_hook_installed(self.vad)
            and self.vad.modes.count(("resume", "speech")) >= speech_round
        )
        self.assertTrue(ok, "conversation loop did not start listening")
        self.vad.emit_event("speech", utterance)
        self.vad.emit_event("silence")


class LocalAsrTurnTest(ConversationLoopTestBase):
    async def test_full_turn_state_and_trace_share_turn_id(self):
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(app_logger, "info") as info:
                task = self._start_loop()
                await self._drive_speech(speech_round=1)
                # 第一轮回复播放完成 → 回到监听
                ok = await _wait_until(lambda: len(self.backend.played) == 1)
                self.assertTrue(ok, "first turn did not complete")

                # 第二轮说退出词
                with mock.patch(
                    "core.services.audio.asr.service.ASRService",
                    **{"asr.return_value": "再见"},
                ):
                    await self._drive_speech(speech_round=2)
                    await asyncio.wait_for(task, timeout=3)

        lines = [str(call.args[0]) for call in info.call_args_list]
        pipeline_lines = [line for line in lines if "state=" in line and "turn=" in line]
        self.assertTrue(
            any("turn=1 state=idle->listening src=local_asr" in line for line in pipeline_lines),
            pipeline_lines,
        )
        self.assertTrue(
            any("turn=1 state=listening->processing" in line for line in pipeline_lines),
            pipeline_lines,
        )
        self.assertTrue(
            any("turn=1 state=processing->speaking" in line for line in pipeline_lines),
            pipeline_lines,
        )
        self.assertTrue(
            any("turn=1 state=speaking->idle outcome=played" in line for line in pipeline_lines),
            pipeline_lines,
        )
        self.assertTrue(
            any("outcome=exit" in line for line in pipeline_lines),
            pipeline_lines,
        )
        trace_lines = [line for line in lines if line.startswith("[TurnTrace]")]
        self.assertEqual(2, len(trace_lines), lines)
        self.assertIn("turn=1", trace_lines[0])
        self.assertIn("turn=2", trace_lines[1])
        self.assertIn("incomplete=0", trace_lines[0])
        # 「说完→ASR 定稿」缺口：本地链路 speech_end_to_asr 已可测
        self.assertIn("speech_end_to_asr_ms=", trace_lines[0])
        self.assertNotIn("speech_end_to_asr_ms=-1.0", trace_lines[0])

    async def test_cancel_at_asr_final_silently_skips_llm_and_tts(self):
        """检查点①（ASR 定稿后）与②（LLM 请求前）：两者之间是同步直连、
        无 await 窗口，取消注入在 ASR 定稿时刻即同时覆盖两个检查点——
        断言 LLM 请求未发出、未起播。②自身的抛出语义另由
        PipelineStateTest.test_checkpoint_raises_when_cancelled 覆盖。"""

        def interrupt():
            pipeline_module.cancel_current_turn("wakeup_interrupt")
            self.controller.stop()

        with mock.patch(
            "core.services.audio.asr.service.ASRService",
            **{"asr.side_effect": lambda *a, **k: (interrupt(), "你好")[1]},
        ):
            with mock.patch.object(app_logger, "error") as error:
                task = self._start_loop()
                await self._drive_speech()
                await asyncio.wait_for(task, timeout=3)

        self.assertEqual([], self.backend.sent, "LLM request must not be sent")
        self.assertEqual([], self.backend.played, "TTS must not be played")
        self.assertEqual([], native_stub.begin_playback_session_calls)
        self.assertEqual([], native_stub.stop_tts_playback_calls)
        error.assert_not_called()

    async def test_cancel_during_playback_stops_stream_and_skips_tail(self):
        def interrupt():
            pipeline_module.cancel_current_turn("wakeup_interrupt")
            self.controller.stop()

        self.backend.cancel_during_play = interrupt
        with mock.patch.object(app_logger, "error") as error:
            task = self._start_loop()
            await self._drive_speech()
            await asyncio.wait_for(task, timeout=3)

        # TTS 已开始（真实世界：Rust 推流中），打断以播放令牌定向杀流收场
        self.assertEqual(1, len(self.backend.played))
        self.assertEqual([7], native_stub.stop_tts_playback_calls)
        # 取消后不再起播任何新音频、不进入下一轮监听（录音只恢复过会话开头那一次）
        self.assertEqual(1, len(native_stub.begin_playback_session_calls))
        self.assertEqual(1, self._start_recording.call_count)
        error.assert_not_called()


class XiaoaiAsrTurnTest(ConversationLoopTestBase):
    async def test_multi_segment_stream_plays_per_segment(self):
        """流式多段消费（ticket 03 收集 + ticket 04 逐段起播）：句段逐段
        过检查点、逐段独立播放（首句起播不等全文），非流式回退路径由
        FakeBackend 基线（单句序列 = 单段一次播放）覆盖。"""

        class MultiSegmentBackend(FakeBackend):
            async def stream_response_segments(self, text: str):
                for segment in ("第一句话了。", "第二句话了。"):
                    yield segment

        backend = MultiSegmentBackend()
        self.backend = backend
        self.controller = _make_controller(backend)
        with mock.patch.object(
            self.controller, "uses_xiaoai_asr", return_value=True
        ):
            task = self._start_loop()
            ok = await _wait_until(
                lambda: ("wake_up", True, True) in self.speaker.calls
            )
            self.assertTrue(ok, "loop did not trigger native listening")
            self.assertTrue(
                self.controller.consume_xiaoai_recognize_result(
                    "d1", "讲个故事", True, False
                )
            )
            ok = await _wait_until(lambda: len(backend.played) == 2)
            self.assertTrue(ok)
            self.assertEqual(
                [("第一句话了。", 7), ("第二句话了。", 7)],
                backend.played,
            )
            self.controller.stop()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)

    async def test_final_result_flows_and_cancel_swallows_next_final(self):
        with mock.patch.object(
            self.controller, "uses_xiaoai_asr", return_value=True
        ):
            with mock.patch.object(pipeline_module.logger, "info") as pipeline_info:
                task = self._start_loop()
                ok = await _wait_until(
                    lambda: ("wake_up", True, True) in self.speaker.calls
                )
                self.assertTrue(ok, "loop did not trigger native listening")

                # 正常轮：定稿 → 请求 → 播放 → 下一轮等待
                consumed = self.controller.consume_xiaoai_recognize_result(
                    "d1", "今天天气怎么样", True, False
                )
                self.assertTrue(consumed)
                ok = await _wait_until(lambda: len(self.backend.played) == 1)
                self.assertTrue(ok, "response was not played")
                self.assertEqual(["今天天气怎么样"], self.backend.sent)

                # 打断第一层（仅置取消，未 stop）：定稿文本到达时被静默吞掉
                pipeline_module.cancel_current_turn("wakeup_interrupt")
                consumed = self.controller.consume_xiaoai_recognize_result(
                    "d2", "还有什么", True, False
                )
                self.assertTrue(consumed)
                self.assertEqual(1, len(self.backend.sent))
                self.assertEqual(1, len(self.backend.played))

                # 打断第二层（controller.stop）：会话闸门拒绝后续定稿
                self.controller.stop()
                consumed = self.controller.consume_xiaoai_recognize_result(
                    "d3", "再问一句", True, False
                )
                self.assertFalse(consumed)

                # task.cancel 以 CancelledError 落地：finally 把 outcome 细化为 cancelled
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3)

        lines = [str(call.args[0]) for call in pipeline_info.call_args_list]
        self.assertTrue(
            any("outcome=cancelled" in line for line in lines),
            lines,
        )


class InterruptWiringTest(unittest.TestCase):
    """on_interrupt / reset_all_sessions 的取消置位（新增面）。

    既有两层机制的回归（controller.stop + task.cancel + 记账清空）由
    test_backend_registry.WakeupRoutingTest 覆盖。
    """

    def test_on_interrupt_cancels_active_turn(self):
        import core.wakeup_session as wakeup_module

        pipeline_module.reset_pipeline()
        pipeline_module.begin_turn("xiaoai_asr")
        with mock.patch.object(wakeup_module, "get_xiaozhi", return_value=None):
            with mock.patch.object(wakeup_module, "get_app", return_value=None):
                with mock.patch.dict(
                    sys.modules,
                    {
                        "core.xiaoai": types.SimpleNamespace(
                            XiaoAI=types.SimpleNamespace(
                                stop_conversation=lambda: None,
                                async_loop=asyncio.new_event_loop(),
                            )
                        )
                    },
                ):
                    wsm = wakeup_module.WakeupSessionManager()
                    wsm._stop_device_playback = mock.AsyncMock()
                    with mock.patch.object(
                        wakeup_module.backend_registry, "names", return_value=[]
                    ):
                        wsm.on_interrupt()
        turn = pipeline_module.current_turn()
        self.assertIsNotNone(turn)
        self.assertTrue(turn.cancelled)
        self.assertEqual("wakeup_interrupt", turn.cancel_reason)
        pipeline_module.reset_pipeline()

    def test_reset_all_sessions_cancels_active_turn(self):
        import core.wakeup_session as wakeup_module

        pipeline_module.reset_pipeline()
        pipeline_module.begin_turn("xiaoai_asr")

        async def scenario():
            with mock.patch.dict(
                sys.modules,
                {
                    "core.xiaoai": types.SimpleNamespace(
                        XiaoAI=types.SimpleNamespace(stop_conversation=lambda: None)
                    )
                },
            ):
                with mock.patch.object(
                    wakeup_module, "get_xiaozhi", return_value=None
                ):
                    wsm = wakeup_module.WakeupSessionManager()
                    wsm._stop_device_playback = mock.AsyncMock()
                    await wsm.reset_all_sessions()

        asyncio.run(scenario())
        turn = pipeline_module.current_turn()
        self.assertIsNotNone(turn)
        self.assertTrue(turn.cancelled)
        self.assertEqual("session_reset", turn.cancel_reason)
        pipeline_module.reset_pipeline()


# ---- TTSRouter 埋点缺口 ----


class TTSRouterTraceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()
        native_stub.begin_playback_session_calls.clear()
        native_stub.stop_tts_playback_calls.clear()
        if isinstance(native_stub.tts_play, mock.AsyncMock):
            native_stub.tts_play.reset_mock(return_value=None)

    def tearDown(self):
        pipeline_module.reset_pipeline()
        turn_trace.reset_turn_trace()

    def _config(self, doubao: dict | None):
        if doubao is None:
            mapping = {}
        else:
            mapping = {"tts.doubao": doubao}
        return types.SimpleNamespace(
            get_app_config=lambda key, default=None: mapping.get(
                key, default if default is not None else {}
            )
        )

    async def test_doubao_success_closes_turn_via_rust_channel(self):
        from core.services.tts.router import TTSRouter

        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(app_logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=2, turn_id=1)
                with mock.patch(
                    "core.services.tts.router.ConfigManager"
                ) as config_manager:
                    config_manager.instance.return_value = self._config(
                        {"app_id": "a", "access_key": "k"}
                    )
                    await TTSRouter.play(
                        "好的",
                        configured_provider="doubao",
                        tts_speaker=None,
                    )

        self.assertGreaterEqual(native_stub.tts_play.await_count, 1)
        lines = [str(call.args[0]) for call in info.call_args_list]
        trace_lines = [line for line in lines if line.startswith("[TurnTrace]")]
        self.assertEqual(1, len(trace_lines), lines)
        fields = dict(
            token.split("=", 1)
            for token in trace_lines[0][len("[TurnTrace] ") :].split(" ")
        )
        self.assertEqual("1", fields["tts_ok"])
        self.assertEqual("0", fields["tts_fallback"])
        self.assertEqual("1", fields["playback_finished"])
        # Rust 通道起播时刻不可观测：accept_to_start 留 -1（口径见 turn_trace 头注）
        self.assertEqual("-1.0", fields["accept_to_start_ms"])
        self.assertIsNone(turn_trace.current_trace())

    async def test_doubao_provider_error_is_recorded(self):
        from core.services.tts.router import TTSRouter

        class RealisticSpeaker(FakeSpeaker):
            """模拟真实 SpeakerManager：fallback 播放时会再记一次
            note_tts_result(ok=True)——首置优先语义下不得覆写 provider 错误。"""

            async def play(self, text=None, url=None, buffer=None, blocking=True, timeout=None):
                await super().play(text, url, buffer)
                turn_trace.note_tts_result(ok=True, fallback=False)
                return True

        fallback_speaker = RealisticSpeaker()
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_ENABLED: "1"}):
            turn_trace.begin_turn("xiaoai_asr", asr_chars=2, turn_id=1)
            with mock.patch(
                "core.services.tts.router.ConfigManager"
            ) as config_manager:
                # 无凭证 → _play_doubao 抛 ValueError → provider 错误 → fallback
                config_manager.instance.return_value = self._config(None)
                with mock.patch(
                    "core.ref.get_speaker", return_value=fallback_speaker
                ):
                    await TTSRouter.play(
                        "好的",
                        configured_provider="doubao",
                        tts_speaker=None,
                    )

        self.assertEqual([("play", "好的", None, None)], fallback_speaker.calls)
        trace = turn_trace.current_trace()
        self.assertIsNotNone(trace, "fallback playback close is the adapter's job")
        summary = trace.summary()
        # 首置优先：fallback 成功不覆写 provider 错误（turn19 类失败必须在埋点可见）
        self.assertEqual(0, summary["tts_ok"])
        self.assertEqual(1, summary["tts_fallback"])


if __name__ == "__main__":
    unittest.main()
