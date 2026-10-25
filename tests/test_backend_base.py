"""BackendManagerBase 契约测试（票 04）。

用最小假传输核（FakeBackend）覆盖基类公共骨架的可观察行为：
记账与清理、send 双入口、等待超时、TTS 播放委托、配置装载与热重载、
每子类记账表隔离。不断言内部调用次序细节。
"""

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

backend_base = importlib.import_module("core.backend_base")
BackendManagerBase = backend_base.BackendManagerBase


class FakeBackend(BackendManagerBase):
    """最小假传输核：可编程返回文本 / 抛错 / 慢响应。"""

    CONFIG_PREFIX = "fake_backend"
    ENABLE_ENV_VARS = ("FAKE_BACKEND_ENABLE",)
    LOG_MODULE = "Fake"

    _canned_response: str | None = "echo"
    _raise: Exception | None = None
    _delay = 0.0
    _marker = ""

    @classmethod
    def _apply_config(cls, config):
        cls._marker = config.get("marker", "")

    @classmethod
    async def _request_completion(cls, text, stats=None):
        cls._last_request_text = text
        if cls._raise is not None:
            raise cls._raise
        if cls._delay:
            await asyncio.sleep(cls._delay)
        return cls._canned_response


def _reset_backend(cls):
    # _initialized 置真：绕过 send 入口的 initialize_from_config 重跑
    # （该路径会按环境变量重算启用位，属既有行为，非本测试面）。
    cls._initialized = True
    cls._reload_listener_registered = False
    cls._enabled = False
    cls._rule_prompt = ""
    cls._rule_prompt_for_skill = ""
    cls._tts_provider = None
    cls._tts_speaker = None
    cls._session_tts_speakers = {}
    cls._tts_speed = 1.0
    cls._response_timeout = 120
    cls._response_events.clear()
    cls._response_texts.clear()
    cls._response_tts_speakers.clear()
    cls.last_error = None
    cls._canned_response = "echo"
    cls._raise = None
    cls._delay = 0.0
    cls._marker = ""
    if hasattr(cls, "_last_request_text"):
        del cls._last_request_text


class FakeBackendTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset_backend(FakeBackend)

    def tearDown(self):
        _reset_backend(FakeBackend)


class SendBookkeepingTest(FakeBackendTestBase):
    async def test_send_with_wait_returns_response_text(self):
        FakeBackend._enabled = True

        result = await FakeBackend.send("你好", wait_response=True)

        self.assertEqual("echo", result)
        self.assertEqual("你好", FakeBackend._last_request_text)
        self.assertEqual({}, FakeBackend._response_events)
        self.assertEqual({}, FakeBackend._response_texts)

    async def test_run_transport_notes_usage_context(self):
        """P2 ticket 03：_run_transport 注入聚合维度（backend/model/session_key）。"""
        turn_trace = importlib.import_module("core.utils.turn_trace")
        FakeBackend._enabled = True
        FakeBackend._session_key = "agent:default:fake"
        self.addCleanup(turn_trace.reset_turn_trace)

        with mock.patch.dict(
            "os.environ", {turn_trace.ENV_TURN_TRACE_ENABLED: "1"}
        ):
            await FakeBackend.send("你好", wait_response=True)
            trace = turn_trace.current_trace()
            self.assertIsNotNone(trace)
            self.assertEqual(trace.backend, "Fake")
            self.assertEqual(trace.model, "")  # 基类默认无模型标签
            self.assertEqual(trace.session_key, "agent:default:fake")
            self.assertEqual(trace.llm_requests, 0)  # 请求计数是子类传输核的职责

    async def test_send_disabled_returns_none_without_request(self):
        result = await FakeBackend.send("你好", wait_response=True)

        self.assertIsNone(result)
        self.assertFalse(hasattr(FakeBackend, "_last_request_text"))

    async def test_send_fire_and_forget_returns_run_id_and_resolves(self):
        FakeBackend._enabled = True

        run_id = await FakeBackend.send("你好", wait_response=False)

        self.assertTrue(run_id)
        for _ in range(100):
            if not FakeBackend._response_events:
                break
            await asyncio.sleep(0.01)
        self.assertEqual({}, FakeBackend._response_events)
        self.assertEqual({}, FakeBackend._response_texts)
        self.assertEqual({}, FakeBackend._response_tts_speakers)

    async def test_transport_error_returns_none_and_records_last_error(self):
        FakeBackend._enabled = True
        FakeBackend._raise = RuntimeError("boom")

        result = await FakeBackend.send("你好", wait_response=True)

        self.assertIsNone(result)
        self.assertEqual("RuntimeError: boom", FakeBackend.last_error)

    async def test_send_and_play_reply_returns_text_and_delegates_tts(self):
        FakeBackend._enabled = True
        router = importlib.import_module("core.services.tts.router")
        with mock.patch.object(
            router.TTSRouter, "play", new=mock.AsyncMock()
        ) as play:
            result = await FakeBackend.send_and_play_reply("你好", wait_response=True)

        self.assertEqual("echo", result)
        play.assert_awaited_once()
        self.assertEqual("echo", play.await_args.args[0])
        self.assertIsNone(play.await_args.kwargs["configured_provider"])

    async def test_close_cancels_pending_waiters(self):
        FakeBackend._enabled = True
        run_id = await FakeBackend.send("你好", wait_response=False)
        waiter = FakeBackend._response_events[run_id]

        await FakeBackend.close()

        self.assertTrue(waiter.cancelled() or waiter.done())
        self.assertEqual({}, FakeBackend._response_events)
        self.assertEqual({}, FakeBackend._response_texts)
        self.assertEqual({}, FakeBackend._response_tts_speakers)


class WaitTimeoutTest(FakeBackendTestBase):
    async def test_wait_response_timeout_returns_none(self):
        FakeBackend._enabled = True
        FakeBackend._delay = 0.5
        FakeBackend._canned_response = None  # 慢且无产物，避免超时后写回文本槽
        FakeBackend._response_timeout = 0.05

        result = await FakeBackend.send("你好", wait_response=True)

        self.assertIsNone(result)
        self.assertEqual({}, FakeBackend._response_events)


class TTSDelegationTest(FakeBackendTestBase):
    async def test_play_response_with_tts_passes_router_arguments(self):
        FakeBackend._tts_provider = "doubao"
        FakeBackend._tts_speed = 1.5
        FakeBackend._tts_speaker = "default-speaker"
        router = importlib.import_module("core.services.tts.router")
        with mock.patch.object(
            router.TTSRouter, "play", new=mock.AsyncMock()
        ) as play:
            await FakeBackend._play_response_with_tts(
                "播报内容", tts_speaker="sage", playback_token=7
            )

        play.assert_awaited_once_with(
            "播报内容",
            configured_provider="doubao",
            tts_speaker="sage",
            tts_speed=1.5,
            playback_token=7,
            log_prefix="Fake",
        )

    async def test_play_response_with_tts_falls_back_to_session_speaker(self):
        FakeBackend._tts_provider = None
        FakeBackend._tts_speed = 1.0
        FakeBackend._tts_speaker = "global-speaker"
        FakeBackend._session_tts_speakers = {}
        router = importlib.import_module("core.services.tts.router")
        with mock.patch.object(
            router.TTSRouter, "play", new=mock.AsyncMock()
        ) as play:
            await FakeBackend._play_response_with_tts("播报内容")

        self.assertEqual("global-speaker", play.await_args.kwargs["tts_speaker"])

    async def test_run_tts_speaker_snapshot_taken_at_send_time(self):
        FakeBackend._enabled = True
        FakeBackend._tts_speaker = "session-speaker"
        router = importlib.import_module("core.services.tts.router")
        with mock.patch.object(
            router.TTSRouter, "play", new=mock.AsyncMock()
        ) as play:
            await FakeBackend.send_and_play_reply("你好", wait_response=True)

        self.assertEqual("session-speaker", play.await_args.kwargs["tts_speaker"])


class ConfigLoadingTest(unittest.TestCase):
    def setUp(self):
        _reset_backend(FakeBackend)
        self.listeners = []
        self.config_manager = types.SimpleNamespace(
            get_app_config=lambda prefix, default: self.config,
            add_reload_listener=self.listeners.append,
        )
        self.config = {}

    def tearDown(self):
        _reset_backend(FakeBackend)

    def _reload(self, enabled=None):
        with mock.patch.object(
            backend_base.ConfigManager, "instance", return_value=self.config_manager
        ):
            FakeBackend.reload_from_config(enabled=enabled)

    def test_reload_reads_config_prefix_and_applies_common_fields(self):
        self.config = {
            "marker": "m1",
            "response_timeout": 42,
            "tts_provider": " Doubao ",
            "tts_speaker": "spk",
            "session_tts_speakers": {"agent:a:x": "a-spk", "": "dropped"},
            "tts_speed": 1.5,
            "rule_prompt": "R1",
            "rule_prompt_for_skill": "R2",
        }

        self._reload(enabled=True)

        self.assertEqual("m1", FakeBackend._marker)
        self.assertEqual(42, FakeBackend._response_timeout)
        self.assertEqual("doubao", FakeBackend._tts_provider)
        self.assertEqual("spk", FakeBackend._tts_speaker)
        self.assertEqual({"agent:a:x": "a-spk"}, FakeBackend._session_tts_speakers)
        self.assertEqual(1.5, FakeBackend._tts_speed)
        self.assertEqual("R1", FakeBackend._rule_prompt)
        self.assertEqual("R2", FakeBackend._rule_prompt_for_skill)

    def test_hot_reload_listener_registered_once(self):
        self._reload(enabled=True)
        self._reload(enabled=True)
        self.assertEqual(1, len(self.listeners))

    def test_enabled_param_overrides_env_var(self):
        self.config = {}
        with mock.patch.dict("os.environ", {"FAKE_BACKEND_ENABLE": "1"}):
            self._reload(enabled=False)
        self.assertFalse(FakeBackend._enabled)

        with mock.patch.dict("os.environ", {"FAKE_BACKEND_ENABLE": "0"}):
            self._reload(enabled=True)
        self.assertTrue(FakeBackend._enabled)

    def test_enabled_resolved_from_env_var_when_param_absent(self):
        self.config = {}
        with mock.patch.dict("os.environ", {"FAKE_BACKEND_ENABLE": "yes"}):
            self._reload()
        self.assertTrue(FakeBackend._enabled)

        with mock.patch.dict("os.environ", {"FAKE_BACKEND_ENABLE": "no"}):
            self._reload()
        self.assertFalse(FakeBackend._enabled)

    def test_enabled_defaults_false_without_env_var(self):
        self.config = {}
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FAKE_BACKEND_ENABLE", None)
            self._reload()
        self.assertFalse(FakeBackend._enabled)


class SubclassIsolationTest(unittest.TestCase):
    def test_bookkeeping_dicts_are_per_subclass(self):
        class A(BackendManagerBase):
            LOG_MODULE = "A"

        class B(BackendManagerBase):
            LOG_MODULE = "B"

        A._response_events["run"] = object()
        A._session_tts_speakers["k"] = "v"

        self.assertEqual({}, B._response_events)
        self.assertEqual({}, B._session_tts_speakers)


if __name__ == "__main__":
    unittest.main()
