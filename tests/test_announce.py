"""announce 播报编排深模块（P0 ticket 02）的契约测试。

全部走 `tests/fake_device.py` 的内存设备边界 + `test_mic_gate.MuteAwareDevice`
的设备真值麦态（event:7 建 mute / event:8 删 / mic_get 读存在性）：
不连真机、不联网、不触 Rust 扩展。

覆盖（对应 ticket 02 验收项）：
  1. 正常播报：双闸门开（KWS 计数 +1、固件闭麦）→ 播放 → 等完成 → 双闸门归还；
  2. 词表命中拒绝：AnnounceRejected、body 不执行、双闸门归还、设备零播报命令；
  3. 豁免参数生效（exempt=True 照常播报）；
  4. body 异常：finally 双闸门归还，异常不吞；
  5. 超时兜底：一直 playing → 到预算按完成处理（reason=timeout）；
  6. 读不到状态判失败（reason=no_observation，finished=False）；
  7. 词源接线：wakeup.keywords（ConfigManager）+ config 模块指令键两组都生效；
  8. /api/play/text 切换 announce：blocking 正常 / 词表拒绝 400 code=announce_rejected /
     非阻塞拒绝在后台不发声（响应语义不变）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_device import CommandResult, FakeXiaoAI, load_speaker_module  # noqa: E402
from test_mic_gate import MuteAwareDevice  # noqa: E402

speaker_module = load_speaker_module()

from core import ref  # noqa: E402
from core.device import playback_events  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

BLOCKED_WORD = "开启高保真"


def _run(coro):
    return asyncio.run(coro)


class _CountingKWS:
    """KWS 最小替身：播放闸门计数真实可观测。"""

    def __init__(self) -> None:
        self.count = 0
        self.reasons: list[str] = []
        self.listen_disabled = False

    def gate_on(self, reason="", max_seconds=120.0, token=None):
        self.count += 1
        self.reasons.append(reason)
        return f"tok-{self.count}"

    def gate_off(self, token=None):
        if self.count > 0:
            self.count -= 1
        if self.reasons:
            self.reasons.pop()

    def is_listening_disabled(self) -> bool:
        return self.listen_disabled


def _context(status: int = 0) -> str:
    return json.dumps(
        {
            "code": 0,
            "info": json.dumps({"status": status, "volume": 39, "player_mode": 1}),
        },
        ensure_ascii=False,
    )


def _synth(path: str = "/tmp/tts/tts_announce_1.mp3") -> str:
    return json.dumps(
        {"code": 0, "info": json.dumps({"path": path}, ensure_ascii=False)},
        ensure_ascii=False,
    )


class AnnounceTestBase(unittest.TestCase):
    """announce 单元面：真 SpeakerManager + 内存设备 + 计数 KWS。"""

    def setUp(self) -> None:
        import open_xiaoai_server  # 由 load_speaker_module 注入的桩

        async def _noop(*_args, **_kwargs):
            return None

        for name in ("stop_playing", "play_audio_file"):
            if not hasattr(open_xiaoai_server, name):
                setattr(open_xiaoai_server, name, _noop)

        ref.GLOBAL_STATES.clear()
        self.device = MuteAwareDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        self.kws = _CountingKWS()
        ref.set_kws(self.kws)
        self.speaker = speaker_module.SpeakerManager()

        playback_events.reset_playback_events()
        self.adapter = playback_events.get_playback_events()
        self._saved_event_plane = self.adapter.configure(
            poll_interval_ms=2, start_grace_ms=10
        )

    def tearDown(self) -> None:
        self.adapter.config = self._saved_event_plane
        playback_events.reset_playback_events()
        ref.GLOBAL_STATES.clear()

    def _blocked(self):
        """词源注入：KWS 词表 + 指令键各含一个可命中的词。"""
        return (
            mock.patch.object(
                speaker_module, "_kws_keywords", lambda: [BLOCKED_WORD, "下一首歌曲"]
            ),
            mock.patch.object(
                speaker_module, "_config_command_keys", lambda: ["打开电脑"]
            ),
        )


class AnnounceNormalPathTest(AnnounceTestBase):
    def test_normal_announce_gates_plays_and_recovers(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", _context(0))  # 空闲 → 等完成立即判定

        async def scenario():
            async with self.speaker.announce(text="客厅音响已就绪", reason="t") as session:
                # 进入后：KWS 闸门持有、固件已闭麦
                self.assertEqual(self.kws.count, 1)
                self.assertTrue(self.device.muted)
                await self.speaker.play(text="客厅音响已就绪", blocking=False)

        _run(scenario())

        # 退出后：双闸门归还、麦恢复快照 on、设备确实收到了播报命令
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)
        self.device.assert_called("text_to_speech")
        self.device.assert_called('"event":7')
        self.device.assert_called('"event":8')

    def test_wait_records_finish_outcome(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", _context(0))  # 一直空闲

        async def scenario():
            async with self.speaker.announce(text="你好", reason="t") as session:
                await self.speaker.play(text="你好", blocking=False)
            return session

        session = _run(scenario())
        self.assertTrue(session.wait_finished)
        self.assertFalse(session.wait_started)
        self.assertIn(session.wait_reason, ("idle", "idle_grace"))

    def test_blocking_caller_with_wait_false_skips_second_wait(self) -> None:
        self.device.on("text_to_speech", _synth())
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code": 0}')
        self.device.on("player_get_context", _context(0))

        async def scenario():
            async with self.speaker.announce(
                text="你好", reason="t", wait=False
            ) as session:
                result = await self.speaker.play(text="你好", blocking=True)
            return result, session

        result, session = _run(scenario())
        self.assertTrue(result)
        self.assertIsNone(session.wait_finished, "wait=False 不得再做第二次等待")


class AnnounceWordListTest(AnnounceTestBase):
    def test_word_hit_rejects_without_playing_and_returns_gates(self) -> None:
        p1, p2 = self._blocked()

        async def scenario():
            executed = False
            with p1, p2:
                with self.assertRaises(speaker_module.AnnounceRejected) as ctx:
                    async with self.speaker.announce(
                        text=f"正在{BLOCKED_WORD}模式", reason="t"
                    ) as session:
                        executed = True
                        await self.speaker.play(text="x", blocking=False)
            return executed, ctx.exception

        executed, exc = _run(scenario())
        self.assertFalse(executed, "被拒播报的 body 不得执行")
        self.assertEqual(exc.word, BLOCKED_WORD)
        self.assertEqual(self.kws.count, 0, "拒绝路径也必须归还 KWS 闸门")
        self.assertFalse(self.device.muted, "拒绝路径也必须归还固件闸门")
        self.device.assert_not_called("text_to_speech")

    def test_exempt_bypasses_word_list_check(self) -> None:
        p1, p2 = self._blocked()
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", _context(0))

        async def scenario():
            with p1, p2:
                async with self.speaker.announce(
                    text=f"正在{BLOCKED_WORD}模式", reason="t", exempt=True
                ):
                    await self.speaker.play(text="x", blocking=False)

        _run(scenario())
        self.device.assert_called("text_to_speech")
        self.assertEqual(self.kws.count, 0)

    def test_body_exception_returns_both_gates(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')

        async def scenario():
            async with self.speaker.announce(text="你好", reason="t"):
                raise RuntimeError("playback exploded")

        with self.assertRaises(RuntimeError):
            _run(scenario())
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    def test_timeout_fallback_counts_as_finished(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", _context(1))  # 一直「播放中」

        async def scenario():
            async with self.speaker.announce(
                text="你好", reason="t", wait_budget_ms=60
            ) as session:
                await self.speaker.play(text="你好", blocking=False)
            return session

        session = _run(scenario())
        self.assertTrue(session.wait_finished, "超时是完成判据的兜底，不是失败")
        self.assertTrue(session.wait_started)
        self.assertEqual(session.wait_reason, "timeout")

    def test_unreadable_status_is_explicit_failure(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", "not json")  # 整个等待期读不到

        async def scenario():
            async with self.speaker.announce(
                text="你好", reason="t", wait_budget_ms=60
            ) as session:
                await self.speaker.play(text="你好", blocking=False)
            return session

        session = _run(scenario())
        self.assertFalse(session.wait_finished, "读不到状态不能按完成算")
        self.assertEqual(session.wait_reason, "no_observation")
        self.assertEqual(self.kws.count, 0)


class WordSourceWiringTest(AnnounceTestBase):
    def test_command_keys_read_from_config_module(self) -> None:
        fake_config = types.ModuleType("config")
        fake_config.DIRECT_COMMANDS = {"音乐模式": ["正在准备客厅音响"]}
        fake_config.XIAOAI_COMMANDS = {"打开电脑": ["正在唤醒客厅电脑"]}

        saved = sys.modules.get("config")
        sys.modules["config"] = fake_config
        try:
            keys = speaker_module._config_command_keys()
        finally:
            if saved is None:
                sys.modules.pop("config", None)
            else:
                sys.modules["config"] = saved
        self.assertIn("音乐模式", keys)
        self.assertIn("打开电脑", keys)

    def test_keywords_read_via_config_manager(self) -> None:
        class _Cfg:
            @classmethod
            def instance(cls):
                return cls()

            def get_app_config(self, path, default=None):
                if path == "wakeup.keywords":
                    return ["你好小智", "停止聆听"]
                return default

        with mock.patch("core.utils.config.ConfigManager", _Cfg, create=True):
            self.assertEqual(
                speaker_module._kws_keywords(), ["你好小智", "停止聆听"]
            )

    def test_find_blocked_word_crosses_both_sources(self) -> None:
        p1, p2 = self._blocked()
        with p1, p2:
            self.assertEqual(
                speaker_module.find_blocked_word(f"正在{BLOCKED_WORD}模式"),
                BLOCKED_WORD,
            )
            self.assertEqual(speaker_module.find_blocked_word("打开电脑"), "打开电脑")
            self.assertIsNone(speaker_module.find_blocked_word("客厅音响已就绪"))
            self.assertIsNone(speaker_module.find_blocked_word(""))
            self.assertIsNone(speaker_module.find_blocked_word(None))

    def test_unavailable_word_sources_degrade_to_no_check(self) -> None:
        # 词源缺失必须降级为不检查 + 告警，绝不能让播报路径崩溃。
        class _Boom:
            @classmethod
            def instance(cls):
                raise RuntimeError("config boom")

        with mock.patch(
            "core.utils.config.ConfigManager", _Boom, create=True
        ):
            self.assertIsNone(speaker_module.find_blocked_word("任意文案"))


# ---------------------------------------------------------------- /api/play/text


class FakeRequest:
    """最小 aiohttp Request 替身（同 test_injection_long_term 惯例）。"""

    def __init__(self, payload=None):
        self._payload = payload
        self.can_read_body = payload is not None
        self.headers = {}
        self.transport = None

    async def json(self):
        return self._payload


class _FakeAdminAPI:
    async def auth_middleware(self, request, handler):
        return await handler(request)

    def register(self, app):
        pass


class _Allow:
    allowed = True
    status = 200
    code = "ok"
    error = ""

    def header_dict(self):
        return {}


class _Tokens:
    errors = ()
    entries = ()
    labels = ()


class _Logger:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _FakeConfigManager:
    @classmethod
    def instance(cls):
        return cls()

    def get_app_config(self, path, default=None):
        return default


def _load_api_server_module():
    """桩掉重依赖后加载 api_server（core.ref 用真身，接真 SpeakerManager）。"""
    stub_names = [
        "open_xiaoai_server",
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
    sys.modules["core.wakeup_session"].EventManager = type("EventManager", (), {})
    sys.modules["core.services.admin_api"].AdminAPI = _FakeAdminAPI

    auth_mod = sys.modules["core.services.auth"]
    auth_mod.authorize = lambda *a, **k: _Allow()
    auth_mod.classify_route = lambda *a, **k: "read"
    auth_mod.configured_tokens = lambda *a, **k: _Tokens()
    sys.modules["core.services.tts.doubao"].DoubaoTTS = type("DoubaoTTS", (), {})
    sys.modules["core.utils.config"].ConfigManager = _FakeConfigManager
    sys.modules["core.utils.logger"].logger = _Logger()

    spec = importlib.util.spec_from_file_location(
        "api_server_under_test_announce",
        ROOT / "core/services/api_server.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


class PlayTextEndpointTest(unittest.IsolatedAsyncioTestCase):
    """/api/play/text 全路径走 announce（tracer 入口）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod, cls._saved = _load_api_server_module()

    @classmethod
    def tearDownClass(cls):
        for name, original in cls._saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def setUp(self) -> None:
        ref.GLOBAL_STATES.clear()
        self.device = MuteAwareDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        self.kws = _CountingKWS()
        ref.set_kws(self.kws)
        self.speaker = speaker_module.SpeakerManager()
        self.server = self.mod.APIServer(host="127.0.0.1", port=9092)

        playback_events.reset_playback_events()
        self.adapter = playback_events.get_playback_events()
        self._saved_event_plane = self.adapter.configure(
            poll_interval_ms=2, start_grace_ms=10
        )
        self._blocked_patches = (
            mock.patch.object(
                speaker_module, "_kws_keywords", lambda: [BLOCKED_WORD]
            ),
            mock.patch.object(speaker_module, "_config_command_keys", lambda: []),
        )

    def tearDown(self) -> None:
        self.adapter.config = self._saved_event_plane
        playback_events.reset_playback_events()
        ref.GLOBAL_STATES.clear()

    def _wire_blocked(self):
        for p in self._blocked_patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._blocked_patches])

    async def test_blocking_word_hit_returns_400_and_never_plays(self) -> None:
        self._wire_blocked()
        resp = await self.server.handle_play_text(
            FakeRequest({"text": f"正在{BLOCKED_WORD}模式", "blocking": True})
        )
        self.assertEqual(resp.status, 400)
        body = json.loads(resp.text)
        self.assertFalse(body["success"])
        self.assertEqual(body["code"], "announce_rejected")
        self.device.assert_not_called("text_to_speech")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    async def test_blocking_normal_plays(self) -> None:
        self.device.on("text_to_speech", _synth())
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code": 0}')
        self.device.on("player_get_context", _context(0))

        resp = await self.server.handle_play_text(
            FakeRequest({"text": "客厅音响已就绪", "blocking": True})
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])
        self.device.assert_called("text_to_speech")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    async def test_nonblocking_word_hit_rejected_in_background(self) -> None:
        self._wire_blocked()
        resp = await self.server.handle_play_text(
            FakeRequest({"text": f"正在{BLOCKED_WORD}模式", "blocking": False})
        )
        # 响应语义与改前一致：非阻塞立即回 success（拒绝发生在后台任务里）
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])

        await asyncio.sleep(0.05)
        self.device.assert_not_called("text_to_speech")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    async def test_nonblocking_normal_plays_and_releases_gates(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", _context(0))

        resp = await self.server.handle_play_text(
            FakeRequest({"text": "客厅音响已就绪", "blocking": False})
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])

        await asyncio.sleep(0.1)
        self.device.assert_called("text_to_speech")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
