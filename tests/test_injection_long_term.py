"""PC 语音注入长期化测试（ticket 07）。

覆盖：
  1. 模块开关判定（config `inject.enabled` / env `INJECT_ENABLE` 覆盖）
  2. 关闭时端点语义：控制/数据面 503 + code=inject_disabled；
     GET /api/inject 与 /api/health 仍 200 并报告 enabled=false
  3. 关闭时中继不启动 :9093（APIServer.start 跳过 + InjectRelay.start 自身守卫）
  4. 开窗原子性：成功 = 闸门 + 中继 + 唤醒同开；wake False/异常/无 speaker/
     中继异常/开闸异常都整窗回滚，不留半开状态
  5. 关窗幂等
  6. TTL 到期自动关窗（真实 EventManager + 中继联动）
  7. 健康字段（enabled/gate/remaining/relay active+stats，不含文本）

不连真机、不监听端口、不压测云端：全部纯内存 asyncio + 桩。
用法：
    python3 tests/test_injection_long_term.py
"""

import asyncio
import importlib.util
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.services.inject_control import (  # noqa: E402
    INJECT_DISABLED_CODE,
    ENV_INJECT_ENABLE,
    open_injection_window,
    resolve_inject_enabled,
)


# ---------------------------------------------------------------- 桩与加载器


class FakeSpeaker:
    """SpeakerManager 替身：记录 wake_up 调用，可返回 False 或抛异常。"""

    def __init__(self, result=True, exc=None):
        self.result = result
        self.exc = exc
        self.calls: list[tuple[bool, bool]] = []

    async def wake_up(self, awake=True, silent=True):
        self.calls.append((awake, silent))
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeRequest:
    """最小 aiohttp Request 替身。"""

    def __init__(self, payload=None, query=None):
        self._payload = payload
        self.can_read_body = payload is not None
        self.query = query or {}
        self.headers = {}
        self.transport = None

    async def json(self):
        return self._payload


class FakeEventManager:
    """EventManager 替身：闸门状态真实可观测，方法调用计数。"""

    INJECTION_GATE_TTL = 30.0
    gate_until = 0.0
    opens = 0
    closes = 0
    last_duration = None

    @classmethod
    def reset(cls):
        cls.gate_until = 0.0
        cls.opens = 0
        cls.closes = 0
        cls.last_duration = None

    @classmethod
    def open_injection_gate(cls, duration=None):
        ttl = cls.INJECTION_GATE_TTL if duration is None else float(duration)
        cls.gate_until = time.time() + ttl if ttl > 0 else 0.0
        cls.opens += 1
        cls.last_duration = duration
        return ttl

    @classmethod
    def close_injection_gate(cls):
        cls.closes += 1
        cls.gate_until = 0.0

    @classmethod
    def injection_gate_active(cls):
        return cls.gate_until > 0 and time.time() < cls.gate_until

    @classmethod
    def injection_gate_remaining(cls):
        return max(0.0, cls.gate_until - time.time()) if cls.gate_until else 0.0


class FakeConfigManager:
    """ConfigManager 替身：`inject.enabled` 由类属性控制。"""

    enabled = False

    @classmethod
    def instance(cls):
        return cls()

    def get_app_config(self, path, default=None):
        if path == "inject.enabled":
            return FakeConfigManager.enabled
        return default


def _load_api_server_module(enabled=False, speaker=None):
    """桩掉重依赖后加载 api_server，返回 (module, saved, runtime)。"""
    stub_names = [
        "open_xiaoai_server",
        "core.ref",
        "core.wakeup_session",
        "core.services.admin_api",
        "core.services.auth",
        "core.services.tts.doubao",
        "core.utils.config",
        "core.utils.logger",
    ]
    saved = {n: sys.modules.get(n) for n in stub_names}
    for name in stub_names:
        sys.modules[name] = types.ModuleType(name)

    sys.modules["open_xiaoai_server"].__getattr__ = lambda n: (lambda *a, **k: None)

    ref = sys.modules["core.ref"]
    ref.get_speaker = lambda: speaker
    ref.get_xiaoai = lambda: None
    ref.get_kws = lambda: None

    # core.wakeup_session: EventManager 替身（闸门状态真实可观测）
    sys.modules["core.wakeup_session"].EventManager = FakeEventManager
    FakeEventManager.reset()

    admin = sys.modules["core.services.admin_api"]

    class _AdminAPI:
        async def auth_middleware(self, request, handler):
            return await handler(request)

        def register(self, app):
            pass

    admin.AdminAPI = _AdminAPI

    auth_mod = sys.modules["core.services.auth"]

    class _Allow:
        allowed = True
        status = 200
        code = "ok"
        error = ""

        def header_dict(self):
            return {}

    auth_mod.authorize = lambda *a, **k: _Allow()
    auth_mod.classify_route = lambda *a, **k: "read"

    class _Tokens:
        errors = ()
        entries = ()
        labels = ()

    auth_mod.configured_tokens = lambda *a, **k: _Tokens()

    sys.modules["core.services.tts.doubao"].DoubaoTTS = type("DoubaoTTS", (), {})

    FakeConfigManager.enabled = bool(enabled)
    sys.modules["core.utils.config"].ConfigManager = FakeConfigManager

    class _Logger:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    sys.modules["core.utils.logger"].logger = _Logger()

    spec = importlib.util.spec_from_file_location(
        "api_server_under_test_07",
        ROOT / "core/services/api_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    runtime = types.SimpleNamespace(
        ref=ref,
        events=FakeEventManager,
        config=FakeConfigManager,
    )
    return module, saved, runtime


def _restore_modules(saved):
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


def _json(resp):
    return json.loads(resp.text)


# ---------------------------------------------------------------- 纯函数：开关判定


class ResolveEnabledTest(unittest.TestCase):
    """`resolve_inject_enabled`：env > config > 默认 false。"""

    def test_default_is_disabled(self):
        self.assertFalse(resolve_inject_enabled(None, env={}))
        self.assertFalse(resolve_inject_enabled({}, env={}))
        self.assertFalse(resolve_inject_enabled({"inject": {}}, env={}))

    def test_dict_config(self):
        self.assertTrue(resolve_inject_enabled({"inject": {"enabled": True}}, env={}))
        self.assertFalse(resolve_inject_enabled({"inject": {"enabled": False}}, env={}))
        # 字符串按布尔解析
        self.assertTrue(resolve_inject_enabled({"inject": {"enabled": "yes"}}, env={}))
        self.assertFalse(resolve_inject_enabled({"inject": {"enabled": "off"}}, env={}))

    def test_config_manager_like_object(self):
        class _Cfg:
            def __init__(self, value):
                self.value = value

            def get_app_config(self, path, default=None):
                return self.value if path == "inject.enabled" else default

        self.assertTrue(resolve_inject_enabled(_Cfg(True), env={}))
        self.assertFalse(resolve_inject_enabled(_Cfg(False), env={}))
        self.assertFalse(resolve_inject_enabled(_Cfg(None), env={}))

    def test_env_overrides_config(self):
        self.assertTrue(resolve_inject_enabled(
            {"inject": {"enabled": False}}, env={ENV_INJECT_ENABLE: "1"}))
        self.assertFalse(resolve_inject_enabled(
            {"inject": {"enabled": True}}, env={ENV_INJECT_ENABLE: "0"}))
        self.assertTrue(resolve_inject_enabled(
            {"inject": {"enabled": False}}, env={ENV_INJECT_ENABLE: "TRUE"}))

    def test_invalid_env_falls_back_to_config(self):
        self.assertTrue(resolve_inject_enabled(
            {"inject": {"enabled": True}}, env={ENV_INJECT_ENABLE: "maybe"}))
        self.assertFalse(resolve_inject_enabled(
            {"inject": {"enabled": False}}, env={ENV_INJECT_ENABLE: ""}))


# ---------------------------------------------------------------- 关闭态端点


class DisabledModuleEndpointsTest(unittest.IsolatedAsyncioTestCase):
    """inject.enabled=false：端点 fail closed，主对话路径不受影响。"""

    @classmethod
    def setUpClass(cls):
        cls.speaker = FakeSpeaker()
        cls.mod, cls._saved, cls.runtime = _load_api_server_module(
            enabled=False, speaker=cls.speaker
        )

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved)

    def setUp(self):
        self.runtime.events.reset()
        # 避免开发者本机 env 干扰
        self._env_saved = os.environ.pop(ENV_INJECT_ENABLE, None)
        self.server = self.mod.APIServer(host="127.0.0.1", port=9092)

    def tearDown(self):
        if self._env_saved is not None:
            os.environ[ENV_INJECT_ENABLE] = self._env_saved

    async def test_post_inject_is_503(self):
        for payload in ({"on": True}, {"on": False}, None):
            resp = await self.server.handle_inject(FakeRequest(payload))
            self.assertEqual(resp.status, 503, payload)
            body = _json(resp)
            self.assertFalse(body["success"])
            self.assertFalse(body["enabled"])
            self.assertEqual(body["code"], INJECT_DISABLED_CODE)

    async def test_get_inject_is_200_with_enabled_false(self):
        resp = await self.server.handle_inject_status(FakeRequest())
        self.assertEqual(resp.status, 200)
        body = _json(resp)
        self.assertTrue(body["success"])
        self.assertFalse(body["enabled"])
        self.assertEqual(body["gate"], "closed")
        self.assertEqual(body["remaining"], 0)
        self.assertFalse(body["relay"]["active"])

    async def test_data_plane_endpoints_are_503(self):
        relay = self.server.inject_relay
        for handler in (relay.handle_upstream, relay.handle_health, relay.handle_ws_text):
            resp = await handler(FakeRequest())
            self.assertEqual(resp.status, 503, handler.__name__)
            self.assertEqual(_json(resp)["code"], INJECT_DISABLED_CODE)

    async def test_health_reports_inject_disabled(self):
        resp = await self.server.handle_health(FakeRequest())
        self.assertEqual(resp.status, 200)
        data = _json(resp)["data"]
        self.assertFalse(data["inject"]["enabled"])
        self.assertEqual(data["inject"]["gate"], "closed")
        self.assertEqual(data["inject"]["remaining"], 0)
        self.assertFalse(data["inject"]["relay"]["active"])
        # 不泄露文本/日志
        self.assertNotIn("_text_log", _json(resp)["data"]["inject"]["relay"])

    async def test_relay_never_listens_when_disabled(self):
        self.assertFalse(self.server.inject_enabled)
        self.assertFalse(self.server.inject_relay.enabled)
        # 直接调 start 也不得创建下游任务（不占 :9093）
        await self.server.inject_relay.start("127.0.0.1", 9093)
        self.assertIsNone(self.server.inject_relay._downstream_task)

    async def test_server_start_skips_relay_when_disabled(self):
        class _FakeRunner:
            def __init__(self, app):
                self.app = app

            async def setup(self):
                pass

            async def cleanup(self):
                pass

        class _FakeSite:
            def __init__(self, *args):
                pass

            async def start(self):
                pass

        fake_start = mock.AsyncMock()
        with mock.patch.object(self.mod.web, "AppRunner", _FakeRunner), \
                mock.patch.object(self.mod.web, "TCPSite", _FakeSite), \
                mock.patch.object(self.server.inject_relay, "start", new=fake_start):
            await self.server.start()
        fake_start.assert_not_called()
        self.assertIsNone(self.server.inject_relay._downstream_task)


# ---------------------------------------------------------------- 开启态开窗


class EnabledWindowTest(unittest.IsolatedAsyncioTestCase):
    """inject.enabled=true：开窗成功、关窗幂等、健康字段。"""

    @classmethod
    def setUpClass(cls):
        cls.speaker = FakeSpeaker(result=True)
        cls.mod, cls._saved, cls.runtime = _load_api_server_module(
            enabled=True, speaker=cls.speaker
        )

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved)

    def setUp(self):
        self.runtime.events.reset()
        self.speaker.calls.clear()
        self._env_saved = os.environ.pop(ENV_INJECT_ENABLE, None)
        self.server = self.mod.APIServer(host="127.0.0.1", port=9092)

    def tearDown(self):
        if self._env_saved is not None:
            os.environ[ENV_INJECT_ENABLE] = self._env_saved

    async def test_open_then_close(self):
        resp = await self.server.handle_inject(FakeRequest({"on": True}))
        self.assertEqual(resp.status, 200)
        body = _json(resp)
        self.assertTrue(body["success"])
        self.assertTrue(body["enabled"])
        self.assertEqual(body["gate"], "open")
        self.assertGreater(body["remaining"], 0)
        self.assertTrue(body["wakeup"])
        # 闸门 + 中继 + 唤醒三者同开
        self.assertTrue(self.runtime.events.injection_gate_active())
        self.assertTrue(self.server.inject_relay.active)
        self.assertEqual(self.speaker.calls, [(True, True)])

        resp = await self.server.handle_inject(FakeRequest({"on": False}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(_json(resp)["gate"], "closed")
        self.assertFalse(self.runtime.events.injection_gate_active())
        self.assertFalse(self.server.inject_relay.active)

    async def test_close_is_idempotent(self):
        for _ in range(3):
            resp = await self.server.handle_inject(FakeRequest({"on": False}))
            self.assertEqual(resp.status, 200)
            body = _json(resp)
            self.assertEqual(body["gate"], "closed")
            self.assertEqual(body["remaining"], 0)
        self.assertFalse(self.runtime.events.injection_gate_active())
        self.assertFalse(self.server.inject_relay.active)

    async def test_custom_duration_is_clamped(self):
        resp = await self.server.handle_inject(
            FakeRequest({"on": True, "duration": 2})
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.runtime.events.last_duration, 2.0)
        # 超过上限 → 夹到 INJECTION_GATE_TTL
        self.server.inject_relay.close_session("test")
        self.runtime.events.reset()
        resp = await self.server.handle_inject(
            FakeRequest({"on": True, "duration": 9999})
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.runtime.events.last_duration, FakeEventManager.INJECTION_GATE_TTL)
        await self.server.handle_inject(FakeRequest({"on": False}))

    async def test_health_reports_gate_and_relay_without_text(self):
        await self.server.handle_inject(FakeRequest({"on": True}))
        resp = await self.server.handle_health(FakeRequest())
        inject = _json(resp)["data"]["inject"]
        self.assertTrue(inject["enabled"])
        self.assertEqual(inject["gate"], "open")
        self.assertGreater(inject["remaining"], 0)
        self.assertTrue(inject["relay"]["active"])
        self.assertEqual(
            set(inject["relay"]["stats"]),
            {"up_bytes", "down_bytes", "sessions", "texts"},
        )
        # 不泄露识别文本
        self.assertNotIn("text", inject["relay"])
        await self.server.handle_inject(FakeRequest({"on": False}))

    async def test_server_start_launches_relay_when_enabled(self):
        class _FakeRunner:
            def __init__(self, app):
                self.app = app

            async def setup(self):
                pass

        class _FakeSite:
            def __init__(self, *args):
                pass

            async def start(self):
                pass

        fake_start = mock.AsyncMock()
        with mock.patch.object(self.mod.web, "AppRunner", _FakeRunner), \
                mock.patch.object(self.mod.web, "TCPSite", _FakeSite), \
                mock.patch.object(self.server.inject_relay, "start", new=fake_start):
            await self.server.start()
        fake_start.assert_awaited_once_with("127.0.0.1", 9093)


# ---------------------------------------------------------------- 开窗原子性


class FakeRelay:
    """中继替身：可模拟 open_session 失败，记录 close_session 调用。"""

    def __init__(self, fail_open=False):
        self.fail_open = fail_open
        self.active = False
        self.closed = []

    def open_session(self):
        if self.fail_open:
            raise RuntimeError("relay boom")
        self.active = True

    def close_session(self, reason="closed"):
        self.active = False
        self.closed.append(reason)


class WindowAtomicityTest(unittest.IsolatedAsyncioTestCase):
    """开闸门 → 开中继 → 唤醒引擎；任一步失败整窗回滚。"""

    @classmethod
    def setUpClass(cls):
        # 本类只用 InjectRelay 类与事件替身；加载一次即可
        cls.mod, cls._saved, cls.runtime = _load_api_server_module(enabled=True)
        cls.InjectRelay = cls.mod.InjectRelay

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved)

    def setUp(self):
        self.runtime.events.reset()

    async def test_success_opens_all_three(self):
        relay = self.InjectRelay(enabled=True)
        speaker = FakeSpeaker(result=True)
        result = await open_injection_window(
            self.runtime.events, relay, speaker, duration=5
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.gate, "open")
        self.assertEqual(result.wakeup, True)
        self.assertEqual(self.runtime.events.last_duration, 5)
        self.assertTrue(self.runtime.events.injection_gate_active())
        self.assertTrue(relay.active)
        self.assertEqual(speaker.calls, [(True, True)])

    async def test_wake_returns_false_rolls_back(self):
        relay = self.InjectRelay(enabled=True)
        speaker = FakeSpeaker(result=False)
        result = await open_injection_window(self.runtime.events, relay, speaker)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "wake_failed")
        self.assertTrue(result.rolled_back)
        self.assertFalse(self.runtime.events.injection_gate_active())
        self.assertFalse(relay.active)
        self.assertEqual(self.runtime.events.closes, 1)

    async def test_wake_exception_rolls_back(self):
        relay = self.InjectRelay(enabled=True)
        speaker = FakeSpeaker(exc=RuntimeError("device boom"))
        result = await open_injection_window(self.runtime.events, relay, speaker)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "wake_failed")
        self.assertIn("device boom", result.error)
        self.assertFalse(self.runtime.events.injection_gate_active())
        self.assertFalse(relay.active)

    async def test_missing_speaker_rolls_back(self):
        relay = self.InjectRelay(enabled=True)
        result = await open_injection_window(self.runtime.events, relay, None)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "speaker_unavailable")
        self.assertFalse(self.runtime.events.injection_gate_active())
        self.assertFalse(relay.active)

    async def test_relay_open_failure_rolls_back(self):
        relay = FakeRelay(fail_open=True)
        speaker = FakeSpeaker(result=True)
        result = await open_injection_window(self.runtime.events, relay, speaker)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "relay_open_failed")
        self.assertFalse(self.runtime.events.injection_gate_active())
        self.assertFalse(relay.active)
        self.assertEqual(relay.closed, ["open window rollback"])
        # 中继失败时不得继续唤醒
        self.assertEqual(speaker.calls, [])

    async def test_missing_relay_rolls_back(self):
        result = await open_injection_window(
            self.runtime.events, None, FakeSpeaker()
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "relay_unavailable")
        self.assertFalse(self.runtime.events.injection_gate_active())

    async def test_gate_failure_does_not_wake(self):
        class _BrokenEvents(FakeEventManager):
            @classmethod
            def open_injection_gate(cls, duration=None):
                raise RuntimeError("gate boom")

            @classmethod
            def close_injection_gate(cls):
                pass

        relay = self.InjectRelay(enabled=True)
        speaker = FakeSpeaker()
        result = await open_injection_window(_BrokenEvents, relay, speaker)
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "gate_open_failed")
        self.assertFalse(relay.active)
        self.assertEqual(speaker.calls, [])


# ---------------------------------------------------------------- TTL 自恢复


def _swap_in_real_event_manager(api_mod):
    """把真实 wakeup_session.EventManager 换入（其 TTL 失效钩子会关中继）。"""
    names = [
        "core.ref",
        "core.utils.config",
        "core.utils.logger",
        "core.services.protocols.typing",
        "core.services.api_server",
    ]
    saved = {n: sys.modules.get(n) for n in names}
    for name in names:
        sys.modules[name] = types.ModuleType(name)

    ref = sys.modules["core.ref"]
    ref.get_app = lambda: None
    ref.get_kws = lambda: None
    ref.get_speaker = lambda: None
    ref.get_xiaozhi = lambda: None
    sys.modules["core.utils.logger"].logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    sys.modules["core.utils.config"].ConfigManager = types.SimpleNamespace(
        instance=lambda: None
    )
    sys.modules["core.services.protocols.typing"].AbortReason = object
    # 真实 EventManager 的 TTL 钩子 `from core.services.api_server import get_relay`
    sys.modules["core.services.api_server"] = api_mod

    spec = importlib.util.spec_from_file_location(
        "wakeup_session_real_07", ROOT / "core/wakeup_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EventManager, saved


class TtlExpiryClosesRelayTest(unittest.IsolatedAsyncioTestCase):
    """PC 崩溃未关窗 → TTL 到期惰性失效，并同步关闭数据面中继。"""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls._saved, cls._runtime = _load_api_server_module(enabled=True)
        cls.InjectRelay = cls.mod.InjectRelay
        cls.EventManager, cls._em_saved = _swap_in_real_event_manager(cls.mod)

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._em_saved)
        _restore_modules(cls._saved)

    def setUp(self):
        # EventManager 是实例，闸门状态保存在类属性上（classmethod 读写 cls）
        type(self.EventManager)._injection_gate_until = 0.0

    async def test_ttl_expiry_closes_gate_and_relay(self):
        relay = self.InjectRelay(enabled=True)
        self.mod._relay_singleton = relay
        self.EventManager.open_injection_gate(5)
        relay.open_session()
        self.assertTrue(self.EventManager.injection_gate_active())
        self.assertTrue(relay.active)

        # 把有效期拨到过去：惰性失效 + 同步关中继
        type(self.EventManager)._injection_gate_until = time.time() - 0.01
        self.assertFalse(self.EventManager.injection_gate_active())
        self.assertFalse(relay.active)
        self.assertEqual(self.EventManager.injection_gate_remaining(), 0)

    async def test_publish_after_ttl_is_dropped(self):
        relay = self.InjectRelay(enabled=True)
        self.mod._relay_singleton = relay
        self.EventManager.open_injection_gate(5)
        relay.open_session()
        type(self.EventManager)._injection_gate_until = time.time() - 0.01
        self.assertFalse(self.EventManager.injection_gate_active())
        relay.publish_text("窗口结束后不应再入队")
        self.assertEqual(relay._text_log, [])
        self.assertEqual(relay.stats["texts"], 0)


if __name__ == "__main__":
    unittest.main()
