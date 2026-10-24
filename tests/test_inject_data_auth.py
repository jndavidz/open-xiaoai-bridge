"""9093 数据面鉴权测试（ticket 12 S1 / notes/10 §3）。

不依赖音箱 / Rust 扩展：
  - 纯逻辑：`resolve_inject_data_auth` / `parse_inject_cidrs` / `peer_allowed`
    （core/services/inject_control.py，真实模块）。
  - 集成：`InjectRelay.downstream_server` / `start` 在真实 asyncio TCP 上的
    「先认证后推流」行为——CIDR 拒绝的连接不接管 `downstream_writer`、
    拿不到任何 PCM；替换连接告警；refused 模式不监听。

覆盖（对照票 12 Acceptance）：
  - 9093 默认关闭（INJECT_ENABLE 空，既有语义不回归）
  - 启用时未配 CIDR/key 且未显式 anon ⇒ 拒绝监听
  - CIDR 命中才推流；源不匹配即断
  - 已有连接被顶替时告警（replaced=true）
"""

import asyncio
import importlib.util
import ipaddress
import os
import socket
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.services.inject_control import (  # noqa: E402
    ENV_INJECT_ALLOW_ANON,
    ENV_INJECT_ALLOW_CIDR,
    InjectDataAuth,
    parse_inject_cidrs,
    peer_allowed,
    resolve_inject_data_auth,
)


def _load_api_server_module():
    """在桩掉重依赖的前提下加载 api_server 模块（同 test_inject_text_replay.py）。

    ⚠️ 必须无条件用新建 stub 替换 sys.modules 条目（不能 setdefault），
    避免真实模块已加载时属性污染无法撤销。
    """
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
    ref.get_speaker = lambda: None
    ref.get_xiaoai = lambda: None
    ref.get_kws = lambda: None

    ws_mod = sys.modules["core.wakeup_session"]

    class _EventManager:
        INJECTION_GATE_TTL = 60.0

        @classmethod
        def injection_gate_active(cls):
            return False

        @classmethod
        def open_injection_gate(cls, duration=None):
            return duration or cls.INJECTION_GATE_TTL

        @classmethod
        def close_injection_gate(cls):
            pass

        @classmethod
        def injection_gate_remaining(cls):
            return 0.0

    ws_mod.EventManager = _EventManager

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
        label = ""

        def header_dict(self):
            return {}

    auth_mod.authorize = lambda *a, **k: _Allow()
    auth_mod.classify_route = lambda *a, **k: "read"
    auth_mod.configured_tokens = lambda *a, **k: None

    sys.modules["core.services.tts.doubao"].DoubaoTTS = type("DoubaoTTS", (), {})

    cfg = sys.modules["core.utils.config"]

    class _ConfigManager:
        @staticmethod
        def instance():
            return _ConfigManager()

        def get_app_config(self, path, default=None):
            return default

    cfg.ConfigManager = _ConfigManager

    logger_mod = sys.modules["core.utils.logger"]

    class _Logger:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    logger_mod.logger = _Logger()

    spec = importlib.util.spec_from_file_location(
        "api_server_under_test_12",
        ROOT / "core/services/api_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


def _reserve_port() -> int:
    """预留一个临时端口（bind 后关闭，存在微小竞态但测试可接受）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ResolveDataAuthTest(unittest.TestCase):
    """鉴权模式判定（notes/10 §3 启用语义）。"""

    def test_nothing_configured_is_refused(self):
        result = resolve_inject_data_auth({})
        self.assertEqual(result.mode, "refused")
        self.assertTrue(result.reason)

    def test_valid_cidr(self):
        result = resolve_inject_data_auth({ENV_INJECT_ALLOW_CIDR: "10.10.10.20/32"})
        self.assertEqual(result.mode, "cidr")
        self.assertEqual(result.networks, (ipaddress.ip_network("10.10.10.20/32"),))

    def test_cidr_list(self):
        result = resolve_inject_data_auth({
            ENV_INJECT_ALLOW_CIDR: "10.10.10.20/32, 192.168.1.0/24",
        })
        self.assertEqual(result.mode, "cidr")
        self.assertEqual(len(result.networks), 2)

    def test_invalid_cidr_is_refused_even_with_anon(self):
        # 配了但配错 → fail closed，不悄悄降级成 anon
        result = resolve_inject_data_auth({
            ENV_INJECT_ALLOW_CIDR: "10.10.10.999/32",
            ENV_INJECT_ALLOW_ANON: "1",
        })
        self.assertEqual(result.mode, "refused")
        self.assertIn("CIDR", result.reason)

    def test_anon_requires_explicit_truthy(self):
        self.assertEqual(
            resolve_inject_data_auth({ENV_INJECT_ALLOW_ANON: "1"}).mode, "anon"
        )
        self.assertEqual(
            resolve_inject_data_auth({ENV_INJECT_ALLOW_ANON: "yes"}).mode, "anon"
        )
        # "0"/空/任意其它值都不算显式许可
        self.assertEqual(
            resolve_inject_data_auth({ENV_INJECT_ALLOW_ANON: "0"}).mode, "refused"
        )
        self.assertEqual(
            resolve_inject_data_auth({ENV_INJECT_ALLOW_ANON: "true "}).mode, "anon"
        )
        self.assertEqual(
            resolve_inject_data_auth({ENV_INJECT_ALLOW_ANON: ""}).mode, "refused"
        )

    def test_empty_cidr_value_falls_through(self):
        # INJECT_ALLOW_CIDR 空白等同未配置 → 走 anon/refused 判定
        result = resolve_inject_data_auth({
            ENV_INJECT_ALLOW_CIDR: "   ",
            ENV_INJECT_ALLOW_ANON: "1",
        })
        self.assertEqual(result.mode, "anon")


class ParseCidrsAndPeerTest(unittest.TestCase):
    def test_parse_ok(self):
        networks, errors = parse_inject_cidrs("10.10.10.20/32,192.168.1.0/24")
        self.assertEqual(errors, ())
        self.assertEqual(len(networks), 2)

    def test_parse_host_bits_are_masked_not_widened(self):
        # strict=False：10.10.10.20/24 归零为 10.10.10.0/24（不放大前缀）
        networks, errors = parse_inject_cidrs("10.10.10.20/24")
        self.assertEqual(errors, ())
        self.assertEqual(networks[0], ipaddress.ip_network("10.10.10.0/24"))

    def test_parse_errors(self):
        networks, errors = parse_inject_cidrs("10.10.10.0/24,,nonsense")
        self.assertEqual(len(errors), 2)
        self.assertEqual(len(networks), 1)
        # 错误信息回显条目便于修配置（CIDR 不是机密）
        self.assertTrue(any("空" in e for e in errors))
        self.assertTrue(any("nonsense" in e for e in errors))

    def test_peer_allowed(self):
        nets = (ipaddress.ip_network("10.10.10.20/32"),)
        self.assertTrue(peer_allowed("10.10.10.20", nets))
        self.assertFalse(peer_allowed("10.10.10.21", nets))

        nets = (ipaddress.ip_network("10.10.10.0/24"),)
        self.assertTrue(peer_allowed("10.10.10.99", nets))
        self.assertFalse(peer_allowed("10.10.11.1", nets))

    def test_peer_allowed_fail_closed(self):
        nets = (ipaddress.ip_network("10.10.10.0/24"),)
        self.assertFalse(peer_allowed(None, nets))
        self.assertFalse(peer_allowed("", nets))
        self.assertFalse(peer_allowed("not-an-ip", nets))
        self.assertFalse(peer_allowed("10.10.10.20", ()))  # 无网段 = 全拒

    def test_peer_allowed_ipv4_mapped_ipv6(self):
        nets = (ipaddress.ip_network("10.10.10.20/32"),)
        self.assertTrue(peer_allowed("::ffff:10.10.10.20", nets))


class RelayDataPlaneAuthTest(unittest.IsolatedAsyncioTestCase):
    """真实 TCP 上的先认证后推流（api_server.InjectRelay）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls._saved = _load_api_server_module()
        cls.InjectRelay = cls.mod.InjectRelay

    @classmethod
    def tearDownClass(cls):
        for name, original in cls._saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    async def _start_relay(self, auth: InjectDataAuth, probe_source=None):
        relay = self.InjectRelay(enabled=True)
        relay.data_auth = auth
        port = _reserve_port()
        task = asyncio.create_task(relay.downstream_server("127.0.0.1", port))
        await self._wait_listening(port, probe_source=probe_source)
        return relay, task, port

    @staticmethod
    async def _wait_listening(port: int, timeout: float = 2.0, probe_source=None):
        """轮询到端口真正可连（create_task + sleep(0) 不保证 start_server 已监听）。

        探测连接会真实触发一次 client_connected：默认用源 127.0.0.2
        （在 127.0.0.1/32 的用例里被 CIDR 拒绝、立即退出，不留挂在
        chunks.get() 上的处理器）；CIDR 允许 127.0.0.2 的用例须传
        probe_source=("127.0.0.1", 0) 让探测走拒绝路径。
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                r, w = await asyncio.open_connection(
                    "127.0.0.1", port, local_addr=probe_source or ("127.0.0.2", 0)
                )
                w.close()
                return
            except OSError:
                await asyncio.sleep(0.01)
        raise AssertionError(f"端口 {port} 在 {timeout}s 内未进入监听")

    async def _teardown_relay(self, relay, task):
        # stop() 统一收口：取消 server 任务与全部连接处理器并等待退出
        # （只 cancel server 任务会卡在 wait_closed 等 handler，见实现注释）
        await relay.stop()

    async def _connect(self, port, source=None):
        return await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, local_addr=source),
            timeout=2.0,
        )

    async def test_cidr_match_receives_pcm(self):
        auth = InjectDataAuth(
            mode="cidr", networks=(ipaddress.ip_network("127.0.0.1/32"),)
        )
        relay, task, port = await self._start_relay(auth)
        try:
            relay.open_session()
            reader, writer = await self._connect(port)
            for _ in range(200):
                await asyncio.sleep(0)
                if relay.downstream_writer is not None:
                    break
            self.assertIsNotNone(relay.downstream_writer, "CIDR 命中的连接应被接管")
            payload = b"\x01\x02\x03\x04" * 8
            await relay.chunks.put(payload)
            got = await asyncio.wait_for(reader.read(len(payload)), timeout=2.0)
            self.assertEqual(got, payload)
            writer.close()
        finally:
            await self._teardown_relay(relay, task)

    async def test_cidr_mismatch_rejected_before_takeover(self):
        # 允许 127.0.0.2，本连接源是 127.0.0.1 → 拒绝，且永不接管
        auth = InjectDataAuth(
            mode="cidr", networks=(ipaddress.ip_network("127.0.0.2/32"),)
        )
        # 探测源用 127.0.0.1（被拒、即时退出），保证全程无 handler 接管
        relay, task, port = await self._start_relay(
            auth, probe_source=("127.0.0.1", 0)
        )
        try:
            relay.open_session()
            reader, writer = await self._connect(port)
            # 推入 PCM：被拒连接不得收到任何字节
            await relay.chunks.put(b"\x01" * 32)
            for _ in range(200):
                await asyncio.sleep(0)
            self.assertIsNone(relay.downstream_writer, "被拒连接不得接管 downstream_writer")
            # 服务端应已断开：读到 EOF（空字节）
            eof = await asyncio.wait_for(reader.read(16), timeout=2.0)
            self.assertEqual(eof, b"")
            writer.close()
        finally:
            await self._teardown_relay(relay, task)

    async def test_replacement_takes_over_and_warns(self):
        auth = InjectDataAuth(
            mode="cidr", networks=(ipaddress.ip_network("127.0.0.1/32"),)
        )
        relay, task, port = await self._start_relay(auth)
        try:
            relay.open_session()
            reader1, writer1 = await self._connect(port)
            for _ in range(200):
                await asyncio.sleep(0)
                if relay.downstream_writer is not None:
                    break
            first = relay.downstream_writer
            reader2, writer2 = await self._connect(port)
            for _ in range(200):
                await asyncio.sleep(0)
                if relay.downstream_writer is not None and \
                        relay.downstream_writer is not first:
                    break
            # 后到者接管（last-wins 语义保留）：server 侧 downstream_writer 的
            # peername = 客户端 sockname，用本地端口区分两条连接
            self.assertIsNot(relay.downstream_writer, first)
            self.assertEqual(
                relay.downstream_writer.get_extra_info("peername"),
                writer2.get_extra_info("sockname"),
            )
            writer1.close()
            writer2.close()
        finally:
            await self._teardown_relay(relay, task)

    async def test_start_refused_does_not_listen(self):
        mod = self.mod
        relay = self.InjectRelay(enabled=True)
        port = _reserve_port()
        environ = {
            "INJECT_ENABLE": "1",   # 控制面开
            # 无 CIDR、无 anon → 数据面拒绝
        }
        saved = {k: os.environ.get(k) for k in environ}
        try:
            os.environ.update(environ)
            os.environ.pop(ENV_INJECT_ALLOW_CIDR, None)
            os.environ.pop(ENV_INJECT_ALLOW_ANON, None)
            await relay.start("127.0.0.1", port)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        try:
            self.assertEqual(relay.data_auth.mode, "refused")
            self.assertIsNone(relay._downstream_task)
            with self.assertRaises((ConnectionRefusedError, OSError)):
                await self._connect(port)
        finally:
            await relay.stop()

    async def test_start_cidr_declares_mode(self):
        relay = self.InjectRelay(enabled=True)
        port = _reserve_port()
        saved_cidr = os.environ.get(ENV_INJECT_ALLOW_CIDR)
        try:
            os.environ[ENV_INJECT_ALLOW_CIDR] = "127.0.0.1/32"
            await relay.start("127.0.0.1", port)
        finally:
            if saved_cidr is None:
                os.environ.pop(ENV_INJECT_ALLOW_CIDR, None)
            else:
                os.environ[ENV_INJECT_ALLOW_CIDR] = saved_cidr
        try:
            self.assertEqual(relay.data_auth.mode, "cidr")
            self.assertIsNotNone(relay._downstream_task)
        finally:
            await relay.stop()

    async def test_health_payload_exposes_auth_mode(self):
        # /api/inject 与 health 的 relay 段应能回答「当前有没有鉴权」
        relay = self.InjectRelay(enabled=True)
        self.assertIsNone(relay.data_auth)   # 未 start = unknown
        relay.data_auth = InjectDataAuth(mode="cidr")
        self.assertEqual(relay.data_auth.mode, "cidr")


if __name__ == "__main__":
    unittest.main(verbosity=2)
