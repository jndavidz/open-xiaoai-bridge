"""后端注册表契约测试（票 07）。

用一个假后端（FakeManager + FakeController）走通「唤醒路由 → 会话启动 →
打断 → 退出」全流程，验证注册表契约：条目派生字段、会话键复位走公开
接口、主控制器通用发送、唤醒会话管理器查表路径。真注册表的三条目
（openclaw/openai/qwenpaw）另测静态形状。
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

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

backend_registry_module = importlib.import_module("core.backend_registry")
backend_registry = backend_registry_module.backend_registry
BackendEntry = backend_registry_module.BackendEntry
INPUT_CONVERSATION = backend_registry_module.INPUT_CONVERSATION
wakeup_session_module = importlib.import_module("core.wakeup_session")
WakeupSessionManager = wakeup_session_module.WakeupSessionManager
app_module = importlib.import_module("core.app")
MainApp = app_module.MainApp
base_module = importlib.import_module("core.backend_base")
BackendManagerBase = base_module.BackendManagerBase


class FakeManager(BackendManagerBase):
    """最小假管理器：记录调用，不碰网络。"""

    CONFIG_PREFIX = "fake_backend_registry"
    ENABLE_ENV_VARS = ("FAKE_REGISTRY_ENABLE",)
    LOG_MODULE = "Fake"
    DEFAULT_SESSION_KEY = "agent:fake:default"
    _rule_prompt = "RULE_PLAY"
    _rule_prompt_for_skill = "RULE_SEND"
    calls: list = []

    @classmethod
    async def send(cls, text, wait_response=False):
        cls.calls.append(("send", text, wait_response))
        return "run-1"

    @classmethod
    async def send_and_play_reply(cls, text, wait_response=False):
        cls.calls.append(("play", text, wait_response))
        return "text-1"


class FakeController:
    """最小假控制器壳：start 挂起直到被打断（或 finish_event 置位自然退出）。"""

    def __init__(self):
        self.started = False
        self.stop_called = False
        self._active = False
        self.consumed = None
        self.finish_event = asyncio.Event()

    async def start(self):
        self.started = True
        self._active = True
        try:
            await self.finish_event.wait()
        finally:
            self._active = False

    def is_active(self):
        return self._active

    def stop(self):
        self.stop_called = True
        self.finish_event.set()

    async def consume_xiaoai_recognize_result(self, **kwargs):
        self.consumed = kwargs
        return True


def _make_fake_registry():
    """构造只含假后端的注册表替身（controller 已解析，绕过点路径导入）。"""
    entry = types.SimpleNamespace(
        name="fake",
        manager=FakeManager,
        load_controller=lambda: FakeController,
        display_name="Fake",
        input_mode=INPUT_CONVERSATION,
    )
    resets: list = []
    return entry, resets, types.SimpleNamespace(
        get=lambda name: entry if name == "fake" else None,
        names=lambda: ("fake",),
        reset_session_keys=lambda cm=None: resets.append(cm),
    )


class RealRegistryShapeTest(unittest.TestCase):
    """真注册表静态形状：三条目 + 派生字段 + 控制器壳可解析。"""

    def test_three_external_backends_registered(self):
        self.assertEqual(
            ("openclaw", "openai", "qwenpaw"), backend_registry.names()
        )

    def test_entry_derives_prefix_and_env_vars_from_manager(self):
        for name in backend_registry.names():
            entry = backend_registry.get(name)
            self.assertEqual(entry.manager.CONFIG_PREFIX, entry.config_prefix)
            self.assertEqual(
                entry.manager.ENABLE_ENV_VARS, entry.enable_env_vars
            )
            self.assertEqual(entry.manager.LOG_MODULE, entry.display_name)
            self.assertEqual(INPUT_CONVERSATION, entry.input_mode)

    def test_controller_shell_loads_and_matches_manager(self):
        for name in backend_registry.names():
            entry = backend_registry.get(name)
            controller_cls = entry.load_controller()
            self.assertIs(entry.manager, controller_cls.MANAGER)

    def test_get_unknown_backend_returns_none(self):
        self.assertIsNone(backend_registry.get("xiaozhi"))
        self.assertIsNone(backend_registry.get("nope"))


class SessionKeyResetTest(unittest.TestCase):
    """会话键复位：走公开接口，消费方不直写私有属性。"""

    def setUp(self):
        self.saved = {
            entry.manager: entry.manager._session_key
            for entry in backend_registry_module.BACKENDS.values()
        }

    def tearDown(self):
        for manager, key in self.saved.items():
            manager._session_key = key

    def test_reset_session_key_reads_config_default(self):
        config_manager = types.SimpleNamespace(
            get_app_config=lambda prefix, default=None: {
                "session_key": "agent:x:y"
            }
        )
        OpenAIManager = backend_registry.get("openai").manager
        OpenAIManager.reset_session_key(config_manager)
        self.assertEqual("agent:x:y", OpenAIManager._session_key)

    def test_reset_session_key_falls_back_to_class_default(self):
        config_manager = types.SimpleNamespace(
            get_app_config=lambda prefix, default=None: {}
        )
        OpenAIManager = backend_registry.get("openai").manager
        OpenAIManager._session_key = "agent:drifted:custom"
        OpenAIManager.reset_session_key(config_manager)
        self.assertEqual(OpenAIManager.DEFAULT_SESSION_KEY, OpenAIManager._session_key)

    def test_reset_session_key_preserves_explicit_empty_string(self):
        # OpenAI 用空串 session_key 禁用会话头——复位不得吞掉显式空串
        config_manager = types.SimpleNamespace(
            get_app_config=lambda prefix, default=None: {"session_key": ""}
        )
        OpenAIManager = backend_registry.get("openai").manager
        OpenAIManager.reset_session_key(config_manager)
        self.assertEqual("", OpenAIManager._session_key)

    def test_registry_reset_session_keys_covers_all_backends(self):
        config_manager = types.SimpleNamespace(get_app_config=lambda p, d=None: {})
        managers = [e.manager for e in backend_registry_module.BACKENDS.values()]
        calls = []
        originals = {}
        for m in managers:
            originals[m] = m.__dict__.get("reset_session_key")

            def recorder(cls, cm=None, _m=m):
                calls.append(_m)

            m.reset_session_key = classmethod(recorder)
        try:
            backend_registry.reset_session_keys(config_manager)
        finally:
            for m in managers:
                if originals[m] is not None:
                    m.reset_session_key = originals[m]
                else:
                    del m.reset_session_key  # 还原为继承基类
        self.assertEqual(managers, calls)


class RegistrySendTest(unittest.IsolatedAsyncioTestCase):
    """主控制器通用发送 + 薄委托（经真注册表注入假条目）。"""

    def setUp(self):
        FakeManager.calls = []
        FakeManager._session_key = FakeManager.DEFAULT_SESSION_KEY
        self._fake_entry = BackendEntry(
            name="fake",
            manager=FakeManager,
            controller_path="tests.test_backend_registry:UnusedHere",
        )
        patcher = mock.patch.dict(
            backend_registry_module.BACKENDS, {"fake": self._fake_entry}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_send_to_backend_appends_skill_rule_and_delegates(self):
        result = await MainApp._send_to_backend(None, "fake", "你好")
        self.assertEqual("run-1", result)
        self.assertEqual([("send", "你好\nRULE_SEND", False)], FakeManager.calls)

    async def test_send_and_play_appends_play_rule(self):
        result = await MainApp._send_to_backend(None, "fake", "你好", and_play=True)
        self.assertEqual("text-1", result)
        self.assertEqual([("play", "你好\nRULE_PLAY", False)], FakeManager.calls)

    async def test_send_wait_response_forwarded(self):
        await MainApp._send_to_backend(None, "fake", "你好", wait_response=True)
        self.assertEqual(True, FakeManager.calls[0][2])

    async def test_send_unknown_backend_returns_none(self):
        result = await MainApp._send_to_backend(None, "nope", "你好")
        self.assertIsNone(result)

    async def test_send_error_returns_none(self):
        async def boom(text, wait_response=False):
            raise RuntimeError("boom")

        with mock.patch.object(FakeManager, "send", boom):
            result = await MainApp._send_to_backend(None, "fake", "你好")
        self.assertIsNone(result)


class WakeupRoutingTest(unittest.IsolatedAsyncioTestCase):
    """假后端走通：唤醒路由 → 会话启动 → ASR 消费 → 打断 → 退出。"""

    def setUp(self):
        self.entry, self.resets, self.fake_registry = _make_fake_registry()
        self.config_stub = types.SimpleNamespace(
            get_app_config=lambda key, default=None: (
                self._before_wakeup if key == "wakeup.before_wakeup" else {}
            )
        )
        self._before_wakeup = self._make_before_wakeup()
        patchers = [
            mock.patch.object(
                wakeup_session_module.ConfigManager, "instance",
                return_value=self.config_stub,
            ),
            mock.patch.object(wakeup_session_module, "get_kws", return_value=None),
            mock.patch.object(
                wakeup_session_module, "backend_registry", self.fake_registry
            ),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])

    def _make_before_wakeup(self):
        async def before_wakeup(speaker, text, source, app):
            return self._route_to
        return before_wakeup

    def _make_wsm(self):
        wsm = WakeupSessionManager()
        wsm.reset_all_sessions = mock.AsyncMock()
        wsm._stop_device_playback = mock.AsyncMock()
        return wsm

    def _fake_xiaoai_stub(self):
        return types.SimpleNamespace(
            XiaoAI=types.SimpleNamespace(
                stop_conversation=lambda: None,
                start_recording=mock.AsyncMock(),
            )
        )

    async def test_wakeup_route_starts_session_and_interrupt_stops(self):
        self._route_to = "fake"
        with mock.patch.dict(sys.modules, {"core.xiaoai": self._fake_xiaoai_stub()}):
            with mock.patch.object(
                wakeup_session_module, "get_app",
                return_value=types.SimpleNamespace(loop=asyncio.get_running_loop()),
            ):
                wsm = self._make_wsm()
                task = asyncio.create_task(wsm.wakeup("你好", "test"))

                controller = None
                for _ in range(200):
                    controller = wsm._controllers.get("fake")
                    if controller and controller.started:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(controller)
                self.assertTrue(controller.started)
                self.assertIn("fake", wsm._tasks)

                # 会话键复位走了注册表公开接口
                self.assertEqual([self.config_stub], self.resets)

                # ASR 结果消费路由到活跃控制器
                self.assertTrue(
                    await wsm.consume_xiaoai_asr_result("d", "hi", True, True)
                )
                self.assertEqual("hi", controller.consumed["text"])

                # 打断：stop + 任务取消 + 记账清空
                wsm.on_interrupt()
                await task
                self.assertTrue(controller.stop_called)
                self.assertEqual({}, wsm._controllers)
                self.assertEqual({}, wsm._tasks)
                self.assertTrue(task.done() and not task.exception())

    async def test_wakeup_route_xiaozhi_branch_does_not_start_backends(self):
        self._route_to = "xiaozhi"
        wsm = self._make_wsm()
        with mock.patch.object(wakeup_session_module, "get_xiaozhi", return_value=None):
            await wsm.wakeup("你好", "test")
        self.assertEqual({}, wsm._controllers)

    async def test_wakeup_unknown_backend_is_ignored(self):
        self._route_to = "nope"
        wsm = self._make_wsm()
        await wsm.wakeup("你好", "test")
        self.assertEqual({}, wsm._controllers)

    async def test_conversation_exits_cleanly_and_clears_bookkeeping(self):
        self._route_to = "fake"
        wsm = self._make_wsm()

        task = asyncio.create_task(wsm._start_backend_conversation("fake"))
        controller = None
        for _ in range(200):
            controller = wsm._controllers.get("fake")
            if controller and controller.started:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(controller.started)

        controller.finish_event.set()  # 自然退出路径（如退出词）
        await task
        self.assertEqual({}, wsm._controllers)
        self.assertEqual({}, wsm._tasks)


if __name__ == "__main__":
    unittest.main()
