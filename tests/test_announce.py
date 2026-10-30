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
from core.commands import ha_script, run_steps  # noqa: E402  P2 ticket 01 内核化后执行器在 core
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
        # B-03：完成证据要带上来源与质量，弱证据不得冒充「完整播完」
        self.assertEqual("weak", session.wait_quality)
        self.assertEqual("context", session.wait_source)

    def test_weak_completion_is_reported_as_weak_evidence(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", _context(0))  # 从未观察到起播

        async def scenario():
            async with self.speaker.announce(text="长文本播报", reason="t") as session:
                await self.speaker.play(text="长文本播报", blocking=False)
            return session

        # 例行的宽限兜底走 DEBUG（不刷屏），但结论必须如实带上质量
        with mock.patch.object(speaker_module.logger, "debug") as debug:
            session = _run(scenario())
        messages = " ".join(str(call.args[0]) for call in debug.call_args_list)
        self.assertIn("weak evidence", messages)
        self.assertIn("quality=weak", messages)
        self.assertEqual("weak", session.wait_quality)

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


class BlockingPlaybackGenerationTest(AnnounceTestBase):
    """§15 #9：阻塞播报的两条路径同样要在下发播放命令**之前**取起播序号。

    缺序号时 `_new_view` 会把缓存里上一段的 playing 预成本段「已在播放」，
    于是「旧 playing + 新 idle」拼成一次假完成：临时文件被提前删、焦点提前归还。
    """

    def setUp(self) -> None:
        super().setUp()
        self.adapter.configure(source="client", poll_interval_ms=2, start_grace_ms=10)

    def _spy(self) -> list:
        seen: list = []
        original = self.adapter.wait_for_finish

        async def spy(
            timeout_ms,
            start_grace_ms=None,
            *,
            poll_interval_ms=None,
            since_seq=None,
        ):
            completion = await original(
                timeout_ms,
                start_grace_ms,
                poll_interval_ms=poll_interval_ms,
                since_seq=since_seq,
            )
            seen.append((since_seq, completion))
            return completion

        mock.patch.object(self.adapter, "wait_for_finish", spy).start()
        self.addCleanup(mock.patch.stopall)
        return seen

    def test_file_playback_binds_seq_before_play_url(self) -> None:
        self.device.on("text_to_speech", _synth())
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code": 0}')

        self.adapter.feed_client_status("playing")  # 上一段播放残留
        expected_seq = self.adapter.begin_playback()
        seen = self._spy()

        async def scenario():
            task = asyncio.ensure_future(
                self.speaker.play(text="客厅音响已就绪", blocking=True, timeout=2000)
            )
            await asyncio.sleep(0.01)
            self.adapter.feed_client_status("idle")
            return await task

        self.assertTrue(_run(scenario()))
        self.assertEqual(1, len(seen))
        since_seq, completion = seen[0]
        self.assertEqual(
            expected_seq,
            since_seq,
            "序号必须在 player_play_url 之前取，否则旧事件会给本段背书",
        )
        self.assertFalse(completion.started, "旧段的 playing 不得预成本段起播")
        self.assertEqual("weak", completion.quality)

    def test_script_fallback_binds_seq_too(self) -> None:
        self.device.on("text_to_speech", '{"code": 1}')  # 合成失败 → 脚本兜底
        self.device.on("tts_play.sh", "", exit_code=1)

        self.adapter.feed_client_status("playing")
        seen = self._spy()

        async def scenario():
            task = asyncio.ensure_future(
                self.speaker.play(text="你好", blocking=True, timeout=2000)
            )
            await asyncio.sleep(0.01)
            self.adapter.feed_client_status("idle")
            return await task

        self.assertTrue(_run(scenario()))
        self.device.assert_called("tts_play.sh")
        self.assertEqual(1, len(seen))
        since_seq, completion = seen[0]
        self.assertIsNotNone(since_seq, "脚本兜底路径同样要绑定起播代次")
        self.assertFalse(completion.started)


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


class _FakeMultipartField:
    def __init__(self, data: bytes):
        self.name = "file"
        self.filename = "hello.mp3"
        self._data = data
        self._sent = False

    async def read_chunk(self, size=8192):
        if self._sent:
            return b""
        self._sent = True
        return self._data


class _FakeMultipartReader:
    def __init__(self, field):
        self._field = field

    async def next(self):
        return self._field


class FakeMultipartRequest(FakeRequest):
    """/api/play/file 的 multipart 替身。"""

    def __init__(self, data: bytes = b"\x00\x01", query=None):
        super().__init__(payload=None)
        self.query = query or {}
        self._reader = _FakeMultipartReader(_FakeMultipartField(data))

    async def multipart(self):
        return self._reader


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


class PlayEndpointsTest(unittest.IsolatedAsyncioTestCase):
    """/api/play/{text,url,file} 全路径走 announce（票 02 tracer + 票 03 全量迁移）。"""

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

    # ---- /api/play/url（P0 ticket 03 迁移）----

    async def test_url_blocking_plays_and_releases_gates(self) -> None:
        self.device.on("miplayer", "", exit_code=0)

        resp = await self.server.handle_play_url(
            FakeRequest({"url": "http://nas/a.mp3", "blocking": True})
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])
        self.device.assert_called("miplayer -f 'http://nas/a.mp3'")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    async def test_url_nonblocking_waits_finish_and_releases_gates(self) -> None:
        self.device.on("player_play_url", '{"code": 0}')
        self.device.on("player_get_context", _context(0))  # 空闲 → 等完成立即判定

        resp = await self.server.handle_play_url(
            FakeRequest({"url": "http://nas/a.mp3", "blocking": False})
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])

        await asyncio.sleep(0.1)
        self.device.assert_called("player_play_url")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    async def test_url_is_not_word_list_checked(self) -> None:
        # URL 内容不可做词表检查（text=None）：即便词表已注入，URL 照常播报。
        self._wire_blocked()
        self.device.on("miplayer", "", exit_code=0)

        resp = await self.server.handle_play_url(
            FakeRequest({"url": "http://nas/开启高保真.mp3", "blocking": True})
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])
        self.device.assert_called("miplayer -f")

    # ---- /api/play/file（P0 ticket 03 迁移）----

    async def test_file_blocking_plays_and_releases_gates(self) -> None:
        resp = await self.server.handle_play_file(
            FakeMultipartRequest(query={"blocking": "true"})
        )
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.text)
        self.assertTrue(body["success"])
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)

    async def test_file_nonblocking_plays_in_background_and_releases_gates(self) -> None:
        resp = await self.server.handle_play_file(
            FakeMultipartRequest(query={"blocking": "false"})
        )
        self.assertEqual(resp.status, 200)
        self.assertTrue(json.loads(resp.text)["success"])

        await asyncio.sleep(0.1)
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)


# ---------------------------------------------------------------- _run_steps（P0 ticket 03）


def _load_deploy_config(module_name: str = "deploy_config_under_test"):
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "deploy" / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunStepsAnnounceTest(AnnounceTestBase):
    """免唤醒/截胡指令执行器的 str 播报走 announce（票 03 验收项 3）。

    P2 ticket 01 内核化后执行器在 core/commands/，本组用例经 deploy/config.py
    的真实表数据（ha_script 构造的步骤元组）驱动 core 执行器，接线同形。
    """

    @classmethod
    def setUpClass(cls):
        cls.deploy = _load_deploy_config()

    def test_run_steps_plays_via_announce_and_keeps_gates_paired(self) -> None:
        self.device.on("text_to_speech", _synth())
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code": 0}')
        self.device.on("player_get_context", _context(0))

        ha_calls: list[tuple] = []
        callable_calls: list[object] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        async def _callable_step(speaker):
            callable_calls.append(speaker)

        steps = [
            "正在准备客厅音响",
            ha_script("hifi_mode"),
            _callable_step,
        ]
        with mock.patch("core.commands.hass.hass_action", _fake_hass):
            _run(run_steps(self.speaker, steps))

        self.device.assert_called("text_to_speech")
        self.device.assert_called('"event":7')
        self.device.assert_called('"event":8')
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)
        self.assertEqual(ha_calls, [("script", "turn_on", {"entity_id": "script.hifi_mode"})])
        self.assertEqual(callable_calls, [self.speaker])

    def test_deployed_command_texts_are_clean_against_deployed_word_sources(self) -> None:
        # 票 03 验收项 3 的自动化：把 deploy/config.py 按运行时同形方式
        # 加载为 config 模块（CONFIG_PATH 机制即如此），断言全部 str 播报
        # 文案对两组词源（deploy 词表 + 指令键）零命中——不需要豁免。
        saved = sys.modules.get("config")
        sys.modules["config"] = _load_deploy_config("config")
        try:
            keywords = self.deploy.APP_CONFIG["wakeup"]["keywords"]
            with mock.patch.object(speaker_module, "_kws_keywords", lambda: keywords):
                # _config_command_keys 不打桩：让它真读 sys.modules["config"]
                # （上面按运行时同形方式加载的 deploy 配置），顺带覆盖其接线。
                for table in (
                    self.deploy.DIRECT_COMMANDS,
                    self.deploy.XIAOAI_COMMANDS,
                ):
                    for key, steps in table.items():
                        for step in steps:
                            if isinstance(step, str):
                                self.assertIsNone(
                                    speaker_module.find_blocked_word(step),
                                    f"{key} 的确认文案命中词表：{step!r}",
                                )
        finally:
            if saved is None:
                sys.modules.pop("config", None)
            else:
                sys.modules["config"] = saved

    def test_word_hit_in_run_steps_rejects_and_stops_sequence(self) -> None:
        # 默认拒绝语义：若未来部署文案与词表冲突，_run_steps 在该步失败、
        # 后续动作不执行——部署者必须在改词表/文案时显式处置，而不是静默豁免。
        self.device.on("text_to_speech", '{"code": 0}')

        ha_calls: list[tuple] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        steps = ["正在准备客厅音响", ha_script("hifi_mode")]
        with (
            mock.patch.object(speaker_module, "_kws_keywords", lambda: ["准备客厅"]),
            mock.patch.object(speaker_module, "_config_command_keys", lambda: []),
            mock.patch("core.commands.hass.hass_action", _fake_hass),
        ):
            with self.assertRaises(speaker_module.AnnounceRejected):
                _run(run_steps(self.speaker, steps))

        self.device.assert_not_called("text_to_speech")
        self.assertEqual(ha_calls, [], "被拒步骤之后的动作不得执行")
        self.assertEqual(self.kws.count, 0)
        self.assertFalse(self.device.muted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
