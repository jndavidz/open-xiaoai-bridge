"""外部文本轮（B-37）控制器与会话占位测试。

覆盖：
  - `_acquire_user_text()` 首分支：预置文本优先于两条 ASR 链路；TurnTrace
    source=external_text + caller；`[ExternalText]` 审计日志行（caller/len/silent）
  - `_conversation_loop()` 外部分支：跳过序前导（断录/notify/开录），单轮后退出
  - `_notify_and_resume_listening()` 外部轮 no-op（无下一轮聆听）
  - `_speak_response_segments()` silent：只收集不发声（不开 channel / 不播咻）
  - 录音恢复：进入时在录且本轮发声断录 ⇒ 收尾 start_recording；silent/本来未录 ⇒ 不动
  - `WakeupSessionManager.begin/finish_external_text_session`：同步占位原子段、
    busy 拒绝（后端会话/xiaozhi）、finish 收尾（pop + kws.resume + resume_after_wakeup）

不连真机：控制器经 `__new__` 构造（同 test_new_topic_wiring），重依赖桩掉。
"""

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

from core.utils import pipeline as conversation_pipeline  # noqa: E402
from core.utils import turn_trace  # noqa: E402
from core.wakeup_session import WakeupSessionManager  # noqa: E402
import core.xiaoai as xiaoai_module  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FakeKws:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self):
        self.pause_calls += 1

    def resume(self):
        self.resume_calls += 1


class FakeController:
    """注册表条目产出的控制器替身：记录 begin_external_turn 参数。"""

    instances: list["FakeController"] = []

    def __init__(self):
        self.begin_calls: list[dict] = []
        self.started = False
        self._external_result = None
        FakeController.instances.append(self)

    def begin_external_turn(self, text, *, source="", silent=False):
        self.begin_calls.append({"text": text, "source": source, "silent": silent})

    def external_result(self):
        return self._external_result

    async def start(self):
        self.started = True
        self._external_result = {"turn_id": 7, "outcome": "played", "reply": "好的"}
        await asyncio.sleep(0)


class FakeEntry:
    display_name = "Fake"

    def load_controller(self):
        return FakeController


class FakeRegistry:
    def __init__(self, names=("openai",)):
        self._names = set(names)

    def get(self, name):
        return FakeEntry() if name in self._names else None

    def reset_session_keys(self, config_manager=None):
        pass


def _controller(external=None):
    c = _controller_cls().__new__(_controller_cls())
    c.LOG_MODULE = "Test Conv"
    c._external_request = external
    c._external_reply = []
    c._external_result = None
    c._pipeline_turn = None
    c._playback_token = None
    c._loop = None
    c._vad_future = None
    c._xiaoai_asr_future = None
    return c


def _controller_cls():
    from core.external_conversation import ExternalConversationController

    return ExternalConversationController


class AcquireExternalTextTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        conversation_pipeline.reset_pipeline()
        turn_trace.reset_turn_trace()
        FakeController.instances.clear()

    async def test_preset_text_returned_before_asr_paths(self):
        c = _controller({"text": "把客厅灯关掉", "source": "cch-screen", "silent": False})
        status, text = await c._acquire_user_text()
        self.assertEqual(status, "ok")
        self.assertEqual(text, "把客厅灯关掉")

    async def test_external_log_line(self):
        c = _controller({"text": "现在几点", "source": "cch-screen", "silent": True})
        with mock.patch("core.external_conversation.logger") as fake_logger:
            status, text = await c._acquire_user_text()
        self.assertEqual((status, text), ("ok", "现在几点"))
        joined = " ".join(str(call.args[0]) for call in fake_logger.info.call_args_list)
        self.assertIn("[ExternalText]", joined)
        self.assertIn("caller=cch-screen", joined)
        self.assertIn("len=4", joined)
        self.assertIn("silent=True", joined)

    async def test_no_vad_or_native_touched(self):
        """外部轮不触碰 VAD / 原生 ASR（预置文本首分支优先）。"""
        c = _controller({"text": "hi", "source": "", "silent": False})
        c._wait_for_speech = mock.AsyncMock(side_effect=AssertionError("VAD path reached"))
        c._wait_for_xiaoai_asr_text = mock.AsyncMock(
            side_effect=AssertionError("native path reached")
        )
        status, text = await c._acquire_user_text()
        self.assertEqual((status, text), ("ok", "hi"))


class CchPreemptUnifiedExitTest(unittest.IsolatedAsyncioTestCase):
    """方案 B 守门：preempt 必须走统一出口（旧 test 只 mock 底层 host_cmd）。"""

    async def test_preempt_calls_unified_exit(self):
        from core.external_conversation import _preempt_cch_silence
        from core.music import cch_cast
        with mock.patch.object(cch_cast, "silence_for_new_intent",
                               new_callable=mock.AsyncMock,
                               return_value=True) as se:
            ok = await _preempt_cch_silence("cch-screen")
        self.assertTrue(ok)
        se.assert_awaited_once()


class CchPreemptiveSilenceTest(unittest.IsolatedAsyncioTestCase):
    """CCH ASR 入口预告静音：覆盖 ASR→LLM tool 决策窗口（真机 ~1.4s）。"""

    async def test_cch_source_sends_fire_and_forget_pause(self):
        from core.external_conversation import _preempt_cch_silence

        with mock.patch("core.music.cch_cast.host_cmd",
                        new_callable=mock.AsyncMock,
                        return_value=(True, "SENT")) as hc:
            ok = await _preempt_cch_silence("cch-screen")
        self.assertTrue(ok)
        hc.assert_awaited_once_with("pause", wait_ack=False)

    async def test_non_cch_source_is_noop(self):
        """LX06 原生语音不误伤——只 CCH source 静音。"""
        from core.external_conversation import _preempt_cch_silence

        with mock.patch("core.music.cch_cast.host_cmd",
                        new_callable=mock.AsyncMock) as hc:
            ok = await _preempt_cch_silence("lx06-asr")
        self.assertFalse(ok)
        hc.assert_not_awaited()


class ConversationLoopDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        conversation_pipeline.reset_pipeline()

    async def test_external_mode_skips_preamble_and_runs_single_turn(self):
        c = _controller({"text": "hi", "source": "", "silent": False})
        c._stop_recording = mock.AsyncMock()
        c._notify_sound = mock.AsyncMock()
        c._start_recording = mock.AsyncMock()
        c._run_external_turn = mock.AsyncMock()
        await c._conversation_loop()
        c._run_external_turn.assert_awaited_once()
        # 序前导（断录→notify→开录）不得执行——外部轮不聆听
        c._stop_recording.assert_not_awaited()
        c._notify_sound.assert_not_awaited()
        c._start_recording.assert_not_awaited()

    async def test_notify_and_resume_noop_in_external_mode(self):
        c = _controller({"text": "hi", "source": "", "silent": False})
        c._notify_sound = mock.AsyncMock()
        c._start_recording = mock.AsyncMock()
        c._wait_for_silence = mock.AsyncMock()
        c.uses_xiaoai_asr = lambda: True  # 跳过 VAD 等待分支
        await c._notify_and_resume_listening()
        c._notify_sound.assert_not_awaited()
        c._start_recording.assert_not_awaited()

    async def test_notify_and_resume_normal_mode_unaffected(self):
        c = _controller(external=None)
        c._notify_sound = mock.AsyncMock()
        c._start_recording = mock.AsyncMock()
        c._wait_for_silence = mock.AsyncMock()
        c.uses_xiaoai_asr = lambda: True
        await c._notify_and_resume_listening()
        c._notify_sound.assert_awaited_once()
        c._start_recording.assert_awaited_once()


class ExternalTurnFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        conversation_pipeline.reset_pipeline()
        turn_trace.reset_turn_trace()

    def _wired(self, external, one_turn_result="continue"):
        c = _controller(external)
        c.calls = []

        async def fake_one_turn():
            c.calls.append("turn")
            return one_turn_result

        c._run_one_turn = fake_one_turn
        return c

    async def test_single_turn_semantics(self):
        """continue 也结束会话：外部轮无下一轮。"""
        c = self._wired({"text": "hi", "source": "", "silent": False})
        await c._run_external_turn()
        self.assertEqual(c.calls, ["turn"])
        self.assertIsNotNone(c._external_result)
        self.assertEqual(c._external_result["outcome"], "played")
        self.assertIsInstance(c._external_result["turn_id"], int)

    async def test_turn_cancelled_outcome(self):
        from core.utils.pipeline import TurnCancelledError

        c = self._wired({"text": "hi", "source": "", "silent": False})

        async def cancelled():
            raise TurnCancelledError(1, "stream_segment", "wakeup_interrupt")

        c._run_one_turn = cancelled
        await c._run_external_turn()
        self.assertTrue(c._external_result["outcome"].startswith("cancelled"))

    async def test_recording_restored_after_playback(self):
        """发声断录（expected 翻 False）⇒ 收尾恢复；KWS resume 才有帧。"""
        c = self._wired({"text": "hi", "source": "", "silent": False})
        c._start_recording = mock.AsyncMock()
        original = xiaoai_module.XiaoAI._recording_expected
        xiaoai_module.XiaoAI._recording_expected = True
        try:

            async def fake_turn():
                # 模拟 speaking_channel 在本轮内物理断录
                xiaoai_module.XiaoAI._recording_expected = False
                return "continue"

            c._run_one_turn = fake_turn
            await c._run_external_turn()
            c._start_recording.assert_awaited_once()
        finally:
            xiaoai_module.XiaoAI._recording_expected = original

    async def test_silent_turn_never_touches_recording(self):
        """silent 轮全程不断录 ⇒ 收尾不动录音通道。"""
        c = self._wired({"text": "hi", "source": "", "silent": True})
        c._start_recording = mock.AsyncMock()
        with mock.patch.object(xiaoai_module.XiaoAI, "_recording_expected", True):
            await c._run_external_turn()
        c._start_recording.assert_not_awaited()

    async def test_not_recording_on_entry_needs_no_restore(self):
        """进入时本就未录（expected=False）⇒ 不再开录（不抢麦克风）。

        覆盖「进入时未录且护栏 bypass 中途断录」组合：was=False ⇒ 无论
        本轮是否发生断录，收尾都不得越权开麦（AGENTS.md mic_gate 快照口径）。
        """
        c = self._wired({"text": "hi", "source": "", "silent": False})
        c._start_recording = mock.AsyncMock()
        original = xiaoai_module.XiaoAI._recording_expected
        xiaoai_module.XiaoAI._recording_expected = False
        try:

            async def fake_turn():
                # 模拟护栏 bypass 播报在本轮内断录
                xiaoai_module.XiaoAI._recording_stop_requested = True
                return "continue"

            c._run_one_turn = fake_turn
            await c._run_external_turn()
            c._start_recording.assert_not_awaited()
        finally:
            xiaoai_module.XiaoAI._recording_expected = original
            xiaoai_module.XiaoAI._recording_stop_requested = False


class SilentSpeakSegmentsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        conversation_pipeline.reset_pipeline()

    @staticmethod
    def _channel():
        class FakeSession:
            def __init__(self):
                self.played = []

            async def play_segment_tts(self, segment):
                self.played.append(segment)

        class FakeCtx:
            def __init__(self, session):
                self.session = session

            async def __aenter__(self):
                return self.session

            async def __aexit__(self, *exc):
                return False

        class FakeChannel:
            def __init__(self):
                self.open_calls = 0
                self.session = FakeSession()

            def open(self, *, reason="", stop_recording=True):
                self.open_calls += 1
                return FakeCtx(self.session)

        return FakeChannel()

    def _speakable(self, segments, external=None):
        c = _controller(external)

        async def stream(text):
            for seg in segments:
                yield seg

        c.backend = types.SimpleNamespace(stream_response_segments=stream)
        c.speaking_channel = self._channel()
        c._play_send_sound = mock.AsyncMock()
        c._loop = asyncio.get_running_loop()
        return c

    async def test_silent_collects_without_speaking(self):
        c = self._speakable(["你好", "今天晴"], external={"text": "天气", "silent": True})
        played = await c._speak_response_segments("天气?", with_send_sound=True)
        self.assertEqual(played, ["你好", "今天晴"])
        self.assertEqual(c.speaking_channel.open_calls, 0)
        c._play_send_sound.assert_not_awaited()
        self.assertEqual(c._external_reply, ["你好", "今天晴"])

    async def test_non_silent_plays_via_channel(self):
        stub = sys.modules["open_xiaoai_server"]
        if not hasattr(stub, "begin_playback_session"):
            stub.begin_playback_session = lambda: object()
        if not hasattr(stub, "stop_tts_playback"):
            stub.stop_tts_playback = lambda token: None
        c = self._speakable(["你好"], external={"text": "天气", "silent": False})
        played = await c._speak_response_segments("天气?", with_send_sound=True)
        self.assertEqual(played, ["你好"])
        self.assertEqual(c.speaking_channel.open_calls, 1)
        c._play_send_sound.assert_awaited_once()
        self.assertEqual(c._external_reply, ["你好"])
        self.assertEqual(c.speaking_channel.session.played, ["你好"])


class StopSkipNativeListeningTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_skips_native_wake_down_in_external_mode(self):
        c = _controller({"text": "hi", "source": "", "silent": False})
        c.active = True
        c.uses_xiaoai_asr = lambda: True
        c._cancel_vad_future = lambda: None
        c._cancel_xiaoai_asr_future = lambda: None
        c._loop = asyncio.get_running_loop()
        with mock.patch("core.external_conversation.get_speaker") as fake_get:
            c.stop()
        fake_get.assert_not_called()  # 外部轮没开过原生聆听，不需要 wake_up(False)

    async def test_stop_normal_mode_still_stops_native(self):
        c = _controller(external=None)
        c.active = True
        c.uses_xiaoai_asr = lambda: True
        c._cancel_vad_future = lambda: None
        c._cancel_xiaoai_asr_future = lambda: None
        c._loop = asyncio.get_running_loop()
        with mock.patch("core.external_conversation.get_speaker") as fake_get:
            c.stop()
        fake_get.assert_called()


class WakeupSessionClaimTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeController.instances.clear()

    def _manager(self):
        mgr = WakeupSessionManager.__new__(WakeupSessionManager)
        mgr._controllers = {}
        mgr._tasks = {}
        mgr._xiaozhi_future = None
        mgr.config = None  # begin_external_text_session 传给 reset_session_keys
        return mgr

    async def test_begin_claims_atomically(self):
        mgr = self._manager()
        kws = FakeKws()
        with mock.patch("core.wakeup_session.backend_registry", FakeRegistry()), mock.patch(
            "core.wakeup_session.get_kws", return_value=kws
        ):
            controller = mgr.begin_external_text_session(
                "openai", text="把灯关掉", source="cch", silent=False
            )
        self.assertIsNotNone(controller)
        self.assertIn("openai", mgr._controllers)
        self.assertEqual(controller.begin_calls, [{"text": "把灯关掉", "source": "cch", "silent": False}])
        self.assertEqual(kws.pause_calls, 1)
        # 会话任务已起动
        await asyncio.sleep(0)
        self.assertTrue(controller.started)

    async def test_begin_busy_when_controller_registered(self):
        mgr = self._manager()
        mgr._controllers["openai"] = object()
        result = mgr.begin_external_text_session("openai", text="hi")
        self.assertIsNone(result)

    async def test_begin_busy_when_xiaozhi_session_active(self):
        mgr = self._manager()
        mgr._xiaozhi_future = asyncio.get_running_loop().create_future()
        result = mgr.begin_external_text_session("openai", text="hi")
        self.assertIsNone(result)

    async def test_begin_unknown_backend_returns_none(self):
        mgr = self._manager()
        with mock.patch("core.wakeup_session.backend_registry", FakeRegistry()):
            self.assertIsNone(mgr.begin_external_text_session("nope", text="hi"))

    async def test_begin_keeps_kws_paused_on_controller_failure(self):
        """构造失败也必须恢复 KWS（不能留在会话暂停态）。"""
        mgr = self._manager()
        kws = FakeKws()

        class BrokenEntry:
            def load_controller(self):
                raise RuntimeError("boom")

        registry = mock.MagicMock()
        registry.get.return_value = BrokenEntry()
        with mock.patch("core.wakeup_session.backend_registry", registry), mock.patch(
            "core.wakeup_session.get_kws", return_value=kws
        ):
            with self.assertRaises(RuntimeError):
                mgr.begin_external_text_session("openai", text="hi")
        self.assertEqual(kws.resume_calls, 1)

    async def test_finish_runs_tail_and_returns_result(self):
        mgr = self._manager()
        kws = FakeKws()
        with mock.patch("core.wakeup_session.backend_registry", FakeRegistry()), mock.patch(
            "core.wakeup_session.get_kws", return_value=kws
        ):
            controller = mgr.begin_external_text_session("openai", text="hi")
            with mock.patch(
                "core.tools.song_request.resume_after_wakeup",
                new=mock.AsyncMock(),
            ) as fake_resume:
                result = await mgr.finish_external_text_session("openai", controller)
        self.assertEqual(result["turn_id"], 7)
        self.assertEqual(result["reply"], "好的")
        self.assertNotIn("openai", mgr._controllers)
        self.assertNotIn("openai", mgr._tasks)
        self.assertEqual(kws.resume_calls, 1)
        fake_resume.assert_awaited_once()

    async def test_finish_does_not_restart_controller(self):
        """回归（C-6 冒烟发现）：silent 等待路径曾对同一控制器二次 start()——
        原轮未完成即读结果（恒 interrupted + 空 reply），且 KWS resume 等尾段
        抢跑。finish 必须只等待 begin 起好的任务，start 全程恰好一次。"""
        mgr = self._manager()
        kws = FakeKws()
        start_calls: list[int] = []

        class CountingController(FakeController):
            async def start(self):
                start_calls.append(1)
                self._external_result = {"turn_id": 1, "outcome": "exit", "reply": ""}
                await asyncio.sleep(0)

        entry = FakeEntry()
        entry.load_controller = lambda: CountingController
        with mock.patch(
            "core.wakeup_session.backend_registry",
            mock.MagicMock(get=lambda name: entry),
        ), mock.patch("core.wakeup_session.get_kws", return_value=kws):
            controller = mgr.begin_external_text_session("openai", text="hi")
            with mock.patch(
                "core.tools.song_request.resume_after_wakeup",
                new=mock.AsyncMock(),
            ):
                result = await mgr.finish_external_text_session("openai", controller)
        self.assertEqual(len(start_calls), 1)
        self.assertEqual(result["outcome"], "exit")
        self.assertEqual(result["turn_id"], 1)

    async def test_finish_result_fallback_when_interrupted(self):
        mgr = self._manager()
        kws = FakeKws()

        class QuietController(FakeController):
            async def start(self):
                await asyncio.sleep(0)  # 不设置 _external_result

        entry = FakeEntry()
        entry.load_controller = lambda: QuietController
        with mock.patch(
            "core.wakeup_session.backend_registry",
            mock.MagicMock(get=lambda name: entry),
        ), mock.patch("core.wakeup_session.get_kws", return_value=kws):
            controller = mgr.begin_external_text_session("openai", text="hi")
            with mock.patch(
                "core.tools.song_request.resume_after_wakeup",
                new=mock.AsyncMock(),
            ):
                result = await mgr.finish_external_text_session("openai", controller)
        self.assertEqual(result["outcome"], "interrupted")
        self.assertEqual(result["reply"], "")

    async def test_conversation_active_reflects_claim(self):
        mgr = self._manager()
        self.assertFalse(mgr.conversation_active())
        with mock.patch("core.wakeup_session.backend_registry", FakeRegistry()), mock.patch(
            "core.wakeup_session.get_kws", return_value=FakeKws()
        ):
            controller = mgr.begin_external_text_session("openai", text="hi")
            self.assertTrue(mgr.conversation_active())
            with mock.patch(
                "core.tools.song_request.resume_after_wakeup", new=mock.AsyncMock()
            ):
                await mgr.finish_external_text_session("openai", controller)
        self.assertFalse(mgr.conversation_active())


class TurnTraceCallerTest(unittest.TestCase):
    """spec 审计要求：source（调用方 id）进 TurnTrace（summary/JSONL）。"""

    def setUp(self):
        turn_trace.reset_turn_trace()

    def tearDown(self):
        turn_trace.reset_turn_trace()

    def test_external_begin_turn_carries_caller(self):
        with mock.patch.dict(os.environ, {"TURN_TRACE_ENABLED": "1"}):
            trace = turn_trace.begin_turn("external_text", asr_chars=4, caller="cch-screen")
            self.assertIsNotNone(trace)
            self.assertEqual(trace.caller, "cch-screen")
            summary = trace.summary()
            self.assertEqual(summary["caller"], "cch-screen")
            self.assertEqual(summary["src"], "external_text")

    def test_normal_turns_keep_caller_empty(self):
        with mock.patch.dict(os.environ, {"TURN_TRACE_ENABLED": "1"}):
            trace = turn_trace.begin_turn("local_asr", asr_chars=3)
            self.assertEqual(trace.caller, "")
            self.assertEqual(trace.summary()["caller"], "")


class ExternalCommandRoutingTest(unittest.IsolatedAsyncioTestCase):
    """外部轮 DIRECT_COMMANDS 路由（C-6 执行面）：表命中执行、不进 LLM。

    外部会话直入（spec ⓒ 绕开 wakeup/before_wakeup），指令表匹配只能在
    `_run_one_turn` 内补；语音路径语义不受影响（表匹配仍在 route_before_wakeup）。
    表来源 = ensure_config_module_loaded()（真实 config 模块）——测试经
    patch.object 换表（sys.modules 桩会被 loader 以 __file__ 校验冲掉）。
    """

    def setUp(self):
        conversation_pipeline.reset_pipeline()
        turn_trace.reset_turn_trace()

    def _stubs(self, c):
        c._checkpoint = lambda *a, **k: None

    async def test_direct_command_hit_runs_steps_skips_llm(self):
        import config as config_real

        c = _controller({"text": "打开相册", "source": "cch-2b", "silent": True})
        self._stubs(c)
        fake_speaker = types.SimpleNamespace()
        played: list[tuple[object, list]] = []

        async def fake_run_steps(speaker, steps):
            played.append((speaker, steps))

        async def must_not_reach(*a, **k):
            raise AssertionError("LLM 编排不应被执行")

        c._try_handle_confirmation = must_not_reach
        with mock.patch.object(config_real, "DIRECT_COMMANDS", {"打开相册": [ha_like_step]}, create=True), \
             mock.patch("core.commands.executor.run_steps", fake_run_steps), \
             mock.patch("core.external_conversation.get_speaker", return_value=fake_speaker):
            result = await c._run_one_turn()
        self.assertEqual(result, "exit")
        self.assertEqual(played, [(fake_speaker, [ha_like_step])])

    async def test_direct_command_miss_falls_through(self):
        import config as config_real

        c = _controller({"text": "今天天气怎么样", "source": "cch-2b", "silent": True})
        self._stubs(c)
        played: list[tuple[object, list]] = []

        async def fake_run_steps(speaker, steps):
            played.append((speaker, steps))

        async def fell_through(*a, **k):
            raise RuntimeError("fell-through marker")

        c._try_handle_confirmation = fell_through
        with mock.patch.object(config_real, "DIRECT_COMMANDS", {"打开相册": [ha_like_step]}, create=True), \
             mock.patch("core.commands.executor.run_steps", fake_run_steps):
            with self.assertRaises(RuntimeError):
                await c._run_one_turn()
        self.assertEqual(played, [])

    async def test_voice_path_semantics_untouched(self):
        """非外部轮（语音路径）不做表匹配——语义仍在 route_before_wakeup。"""
        import config as config_real

        c = _controller(None)
        self._stubs(c)
        played: list[tuple[object, list]] = []

        async def fake_run_steps(speaker, steps):
            played.append((speaker, steps))

        async def fell_through(*a, **k):
            raise RuntimeError("fell-through marker")

        async def acquire_ok(text="打开相册"):
            return ("ok", text)

        c._acquire_user_text = acquire_ok
        c.config = types.SimpleNamespace(get_app_config=lambda path, default=None: default)
        c._try_handle_media_command = fell_through
        with mock.patch.object(config_real, "DIRECT_COMMANDS", {"打开相册": [ha_like_step]}, create=True), \
             mock.patch("core.commands.executor.run_steps", fake_run_steps):
            with self.assertRaises(RuntimeError):
                await c._run_one_turn()
        self.assertEqual(played, [])


def ha_like_step():
    return None


if __name__ == "__main__":
    unittest.main()
