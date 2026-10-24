"""InjectRelay 文本回放测试（B-09：WS 晚连丢文本）。

不依赖音箱 / Rust 扩展 / 网络：`InjectRelay` 的文本通路是纯内存 asyncio
（`str` → `asyncio.Queue` → WebSocket JSON 帧），全程无音频、无设备。

覆盖：
  1. 晚连能拿到历史（核心缺陷：publish 早于订阅）
  2. 回放一定早于实时（顺序陷阱，原方案 `create_task` 先于重放）
  3. seq 单调递增且可去重
  4. since 断点续取只回放更新的部分
  5. 有界裁剪（MAX_TEXT_LOG）不无限增长
  6. 跨窗口保留（原 `text_buffer` 在 open_session 里 clear 的缺陷）
  7. 窗口关闭后 publish_text 不入队（闸门语义不回归）
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_api_server_module():
    """在桩掉重依赖的前提下加载 api_server 模块。

    ⚠️ 必须无条件用新建 stub 替换 sys.modules 条目，不能 `setdefault`：
    真实模块已加载时，后续属性赋值会直接改写真实模块对象，
    tearDownClass 把同一对象赋回 sys.modules 无法撤销属性污染
    （与 test_injection_gate.py 踩过的同一个坑）。
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

    # open_xiaoai_server: 仅供模块顶层 import
    sys.modules["open_xiaoai_server"].__getattr__ = lambda n: (lambda *a, **k: None)

    # core.ref
    ref = sys.modules["core.ref"]
    ref.get_speaker = lambda: None
    ref.get_xiaoai = lambda: None
    ref.get_kws = lambda: None

    # core.wakeup_session: EventManager 的闸门类方法
    ws_mod = sys.modules["core.wakeup_session"]

    class _EventManager:
        INJECTION_GATE_TTL = 60.0
        _until = 0.0

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

    # admin_api: 需要 auth_middleware + register
    admin = sys.modules["core.services.admin_api"]

    class _AdminAPI:
        async def auth_middleware(self, request, handler):
            return await handler(request)

        def register(self, app):
            pass

    admin.AdminAPI = _AdminAPI

    # auth（业务 API 鉴权）：本测试不验证鉴权，桩掉即可
    auth_mod = sys.modules["core.services.auth"]

    class _Allow:
        allowed = True

    auth_mod.authorize = lambda *a, **k: _Allow()
    auth_mod.configured_token = lambda *a, **k: ""

    # tts.doubao
    sys.modules["core.services.tts.doubao"].DoubaoTTS = type("DoubaoTTS", (), {})

    # config
    cfg = sys.modules["core.utils.config"]

    class _ConfigManager:
        @staticmethod
        def instance():
            return _ConfigManager()

        def get_app_config(self, path, default=None):
            return default

    cfg.ConfigManager = _ConfigManager

    # logger
    logger_mod = sys.modules["core.utils.logger"]

    class _Logger:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    logger_mod.logger = _Logger()

    spec = importlib.util.spec_from_file_location(
        "api_server_under_test",
        ROOT / "core/services/api_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


class FakeWS:
    """最小 WebSocket 替身：只记录发出的 JSON 与迭代结束时机。"""

    def __init__(self):
        self.sent: list[dict] = []
        self._closed = asyncio.Event()

    async def prepare(self, request):
        pass

    async def send_json(self, obj):
        # 模拟真实 send_json 的 await 让出点（顺序陷阱的关键）
        await asyncio.sleep(0)
        self.sent.append(obj)

    async def close(self):
        self._closed.set()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        await self._closed.wait()
        return
        yield  # pragma: no cover  (使其成为 async generator)

    def texts(self):
        """按发送顺序返回 (seq, text, is_final) 列表。"""
        return [
            (o["seq"], o["text"], o.get("is_final"))
            for o in self.sent
            if o.get("event") == "text"
        ]


class FakeRequest:
    def __init__(self, query=None):
        self.query = query or {}


class _TextReplayTestBase(unittest.IsolatedAsyncioTestCase):
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

    async def _settle(self, relay, ws, expected_at_least=None, max_turns=2000):
        """推进事件循环，直到订阅建立（且回放条数达标）。

        ⚠️ 不能用固定次数的 ``sleep(0)``：回放每条文本都有一次 ``await``，
        回放量（最多 MAX_TEXT_LOG=200 条）决定了需要的让出次数。
        ``text_queues`` 非空即表示已进入实时阶段。
        """
        for _ in range(max_turns):
            await asyncio.sleep(0)
            established = bool(relay.text_queues)
            enough = (
                expected_at_least is None
                or len(ws.texts()) >= expected_at_least
            )
            if established and enough:
                return
        raise AssertionError(
            f"订阅未在 {max_turns} 次让出内建立 "
            f"(queues={len(relay.text_queues)}, sent={len(ws.texts())})"
        )

    async def _connect(self, relay, since=None, publish_before=(), wait_texts=None):
        """建立一条订阅：先（可选）publish 若干文本，再连 WS，返回 (task, ws)。"""
        for t in publish_before:
            relay.publish_text(t)
        ws = FakeWS()
        parsed = relay.parse_since(None if since is None else str(since))
        task = asyncio.create_task(
            relay._serve_text_subscription(ws, parsed)
        )
        await self._settle(relay, ws, expected_at_least=wait_texts)
        return task, ws

    async def _stop(self, relay, task, ws):
        await ws.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class LateSubscriptionTest(_TextReplayTestBase):
    """核心缺陷：publish 早于订阅 ⇒ 文本不能丢。"""

    async def test_late_ws_receives_earlier_text(self):
        relay = self.InjectRelay()
        relay.open_session()
        # 关键场景：ASR 先出结果，WS 后连
        relay.publish_text("第一句")
        relay.publish_text("第二句")

        task, ws = await self._connect(relay)
        self.assertEqual([t for _, t, _ in ws.texts()], ["第一句", "第二句"])
        await self._stop(relay, task, ws)

    async def test_replay_precedes_realtime(self):
        """顺序陷阱：回放必须早于实时推送。"""
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("历史")

        ws = FakeWS()
        task = asyncio.create_task(relay._serve_text_subscription(ws, 0))
        await self._settle(relay, ws, expected_at_least=1)

        # 订阅已建立后立刻推实时
        relay.publish_text("实时")
        for _ in range(50):
            await asyncio.sleep(0)
            if len(ws.texts()) >= 2:
                break

        self.assertEqual([t for _, t, _ in ws.texts()], ["历史", "实时"])
        await self._stop(relay, task, ws)


class SequenceTest(_TextReplayTestBase):
    """seq 语义：单调递增、可去重、支持 since 断点续取。"""

    async def test_seq_monotonic_and_unique(self):
        relay = self.InjectRelay()
        relay.open_session()
        for i in range(5):
            relay.publish_text(f"第{i}句")

        task, ws = await self._connect(relay)
        seqs = [s for s, _, _ in ws.texts()]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        self.assertEqual(len(seqs), len(set(seqs)), "seq 必须唯一，客户端才能去重")
        await self._stop(relay, task, ws)

    async def test_since_returns_only_newer(self):
        relay = self.InjectRelay()
        relay.open_session()
        for i in range(1, 6):
            relay.publish_text(f"t{i}")

        task, ws = await self._connect(relay, since=3)
        self.assertEqual([(s, t) for s, t, _ in ws.texts()], [(4, "t4"), (5, "t5")])
        await self._stop(relay, task, ws)

    async def test_since_beyond_head_returns_nothing(self):
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("only")

        task, ws = await self._connect(relay, since=99)
        self.assertEqual(ws.texts(), [])
        await self._stop(relay, task, ws)

    async def test_invalid_since_falls_back_to_full_replay(self):
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("a")

        ws = FakeWS()
        invalid = relay.parse_since("not-a-number")
        self.assertEqual(invalid, 0, "非法 since 必须回退为 0（全量回放）")
        task = asyncio.create_task(relay._serve_text_subscription(ws, invalid))
        await self._settle(relay, ws, expected_at_least=1)
        self.assertEqual([t for _, t, _ in ws.texts()], ["a"])
        await self._stop(relay, task, ws)


class BoundedLogTest(_TextReplayTestBase):
    """有界裁剪：内存不能无限增长。"""

    async def test_log_is_bounded(self):
        relay = self.InjectRelay()
        relay.open_session()
        limit = relay.MAX_TEXT_LOG
        for i in range(limit + 50):
            relay.publish_text(f"x{i}")
        self.assertEqual(len(relay._text_log), limit)

    async def test_oldest_dropped_newest_kept(self):
        relay = self.InjectRelay()
        relay.open_session()
        limit = relay.MAX_TEXT_LOG
        for i in range(limit + 5):
            relay.publish_text(f"x{i}")

        task, ws = await self._connect(relay, wait_texts=limit)
        got = [t for _, t, _ in ws.texts()]
        self.assertEqual(len(got), limit)
        # 最新一条必须还在
        self.assertEqual(got[-1], f"x{limit + 4}")
        # 最早的必须已被裁掉
        self.assertNotIn("x0", got)
        await self._stop(relay, task, ws)


class CrossWindowTest(_TextReplayTestBase):
    """跨窗口保留：原 `text_buffer.clear()` 在 open_session 清历史。"""

    async def test_text_survives_new_window(self):
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("窗口A的文本")
        relay.close_session("test")

        # 用户松开快捷键后工具才连 WS：此时窗口已关、新窗口未开
        task, ws = await self._connect(relay)
        self.assertEqual([t for _, t, _ in ws.texts()], ["窗口A的文本"],
                         "文本不应随窗口清空")
        await self._stop(relay, task, ws)

    async def test_text_survives_open_session_clear(self):
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("A")
        relay.close_session("test")

        relay.open_session()   # 新窗口开启——不得清历史
        self.assertEqual(len(relay._text_log), 1)

        task, ws = await self._connect(relay)
        self.assertEqual([t for _, t, _ in ws.texts()], ["A"])
        await self._stop(relay, task, ws)


class GateSemanticsTest(_TextReplayTestBase):
    """闸门语义不回归：窗口关闭时 publish 不入队。"""

    async def test_publish_ignored_when_window_closed(self):
        relay = self.InjectRelay()
        # 未开窗
        relay.publish_text("should be dropped")
        self.assertEqual(relay._text_log, [])
        self.assertEqual(relay.stats["texts"], 0)

    async def test_empty_text_ignored(self):
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("")
        relay.publish_text(None)
        self.assertEqual(relay._text_log, [])

    async def test_multiple_subscribers_all_receive(self):
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("历史")

        task1, ws1 = await self._connect(relay)
        task2, ws2 = await self._connect(relay)
        relay.publish_text("实时")
        for _ in range(50):
            await asyncio.sleep(0)
            if all(any(t == "实时" for _, t, _ in ws.texts()) for ws in (ws1, ws2)):
                break

        for ws in (ws1, ws2):
            self.assertIn("实时", [t for _, t, _ in ws.texts()])
        await self._stop(relay, task1, ws1)
        await self._stop(relay, task2, ws2)


class IsFinalTest(_TextReplayTestBase):
    """`is_final` 透传（2026-10-21 真机实测驱动）。

    真机证据：对「今天天气怎么样」一句话，云端发 **8 条** RecognizeResult，
    且为**累积快照**（不是增量）：

        1-2  "":                        is_final=false
        3    "今":                      is_vad_begin=true
        4    "今天天气":                  is_final=false
        5    "今天天气怎么":              is_final=false
        6    "今天天气怎么样":            is_final=false
        7    "今天天气怎么样":            is_nlp_request=true, is_stop=true
        8    "今天天气怎么样":            **is_final=true**

    ⇒ 必须透传 is_final：否则 PC 端无法判断"说完了"，且第 7/8 条文本相同会重复插入。
    """

    # 真机实测序列（text, is_final）
    REAL_DEVICE_SEQUENCE = [
        ("今", False),
        ("今天天气", False),
        ("今天天气怎么", False),
        ("今天天气怎么样", False),
        ("今天天气怎么样", False),
        ("今天天气怎么样", True),
    ]

    async def test_is_final_passed_through(self):
        relay = self.InjectRelay()
        relay.open_session()
        for text, final in self.REAL_DEVICE_SEQUENCE:
            relay.publish_text(text, is_final=final)

        task, ws = await self._connect(relay, wait_texts=len(self.REAL_DEVICE_SEQUENCE))
        got = ws.texts()

        self.assertEqual(len(got), len(self.REAL_DEVICE_SEQUENCE))
        # 只有最后一条 is_final=True
        self.assertEqual([f for _, _, f in got],
                         [f for _, f in self.REAL_DEVICE_SEQUENCE])
        self.assertEqual(sum(1 for _, _, f in got if f), 1,
                         "一句话只应有一条 is_final=True")
        # 文本是累积快照：最后一条是完整文本
        self.assertEqual(got[-1][1], "今天天气怎么样")

    async def test_default_is_final_is_false(self):
        """不传 is_final 时默认为 False（向后兼容旧调用方）。"""
        relay = self.InjectRelay()
        relay.open_session()
        relay.publish_text("legacy")

        task, ws = await self._connect(relay, wait_texts=1)
        self.assertEqual(ws.texts(), [(1, "legacy", False)])
        await self._stop(relay, task, ws)

    async def test_is_final_survives_replay(self):
        """晚连回放时 is_final 不能丢（否则客户端无法判断终态）。"""
        relay = self.InjectRelay()
        relay.open_session()
        for text, final in self.REAL_DEVICE_SEQUENCE:
            relay.publish_text(text, is_final=final)

        # 全部历史都在，晚连者必须能看到哪条是 final
        task, ws = await self._connect(relay, wait_texts=len(self.REAL_DEVICE_SEQUENCE))
        finals = [(seq, t) for seq, t, f in ws.texts() if f]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0][1], "今天天气怎么样")
        await self._stop(relay, task, ws)

    async def test_since_replay_preserves_is_final(self):
        relay = self.InjectRelay()
        relay.open_session()
        for text, final in self.REAL_DEVICE_SEQUENCE:
            relay.publish_text(text, is_final=final)

        # 从倒数第二条之后续取：只应拿到最后一条（且 is_final=True）
        task, ws = await self._connect(relay, since=len(self.REAL_DEVICE_SEQUENCE) - 1,
                                       wait_texts=1)
        self.assertEqual(ws.texts(), [(len(self.REAL_DEVICE_SEQUENCE),
                                       "今天天气怎么样", True)])
        await self._stop(relay, task, ws)


if __name__ == "__main__":
    unittest.main()
