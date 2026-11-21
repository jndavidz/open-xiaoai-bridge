"""CCH 语音播控目标路由（T2 ticket 08）契约测试。

验证：
- target 两层判定（LLM 显式优先 / 本轮上下文兜底 / 默认 lx06）；
- 上下文 set/reset 不串轮；
- cch_cast.cast() 经 Bearer 投 /cch/cast，revoke() 经 managed/command 撤票。
不连设备、不联网（aiohttp TestServer 伪造）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from core.tools import song_request as sr  # noqa: E402
from core.music import cch_cast  # noqa: E402


class TargetResolutionTest(unittest.TestCase):
    def setUp(self):
        sr.reset_playback_target()

    def tearDown(self):
        sr.reset_playback_target()

    def test_default_is_lx06(self):
        self.assertEqual(sr._resolve_target(None), "lx06")

    def test_context_fallback(self):
        sr.set_playback_target("cch")
        self.assertEqual(sr._resolve_target(None), "cch")

    def test_context_cch_ignores_stray_lx06(self):
        # 2026-11-21：CCH 上下文优先；无原话点名设备时模型显式 lx06 视为误判
        sr.set_playback_target("cch")
        self.assertEqual(sr._resolve_target("lx06"), "cch")
        sr.set_playback_target("lx06")
        self.assertEqual(sr._resolve_target("cch"), "cch")

    def test_context_cch_honours_lx06_when_utterance_names_device(self):
        sr.set_playback_target("cch", text="在小爱音箱上播放晴天")
        self.assertEqual(sr._resolve_target("lx06"), "lx06")

    def test_context_reset(self):
        sr.set_playback_target("cch")
        sr.reset_playback_target()
        self.assertEqual(sr._resolve_target(None), "lx06")

    def test_invalid_values_ignored(self):
        sr.set_playback_target("garbage")  # setter 白名单拒绝
        self.assertEqual(sr._resolve_target(None), "lx06")
        self.assertEqual(sr._resolve_target("garbage"), "lx06")


class CchCastChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_cast_sends_bearer_and_url(self):
        seen = {}

        async def cast_handler(req):
            seen["auth"] = req.headers.get("Authorization", "")
            seen["body"] = await req.json()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/cch/cast", cast_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            orig_url = cch_cast.cch_cast_url
            orig_token = cch_cast.panel_token
            cch_cast.cch_cast_url = lambda: str(client.make_url("/cch/cast"))
            cch_cast.panel_token = lambda: "test-panel-token"
            ok = await cch_cast.cast("http://10.10.10.2:58091/media?media_ticket=abc")
            self.assertTrue(ok)
            self.assertEqual(seen["auth"], "Bearer test-panel-token")
            self.assertEqual(seen["body"]["url"],
                             "http://10.10.10.2:58091/media?media_ticket=abc")
        finally:
            cch_cast.cch_cast_url = orig_url
            cch_cast.panel_token = orig_token
            await client.close()

    async def test_cast_no_token_returns_false(self):
        orig_token = cch_cast.panel_token
        cch_cast.panel_token = lambda: ""
        try:
            ok = await cch_cast.cast("http://10.10.10.2:58091/media?media_ticket=abc")
            self.assertFalse(ok)
        finally:
            cch_cast.panel_token = orig_token

    async def test_revoke_502_is_ticket_revoked(self):
        """songloft: bff.Execute 先撤票、forwardCommand 后才 502 —— 502 视为撤票成功。"""
        import core.music.song_bridge as sb

        orig_token = sb._songloft_token
        orig_cfg = sb._songloft_cfg
        bodies = []

        async def fake_token(base, http):
            return "jwt"

        async def revoke_handler(req):
            bodies.append(await req.json())
            return web.json_response({"error": "bridge session not established"}, status=502)

        app = web.Application()
        app.router.add_post("/api/v1/managed/command", revoke_handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            base = str(client.make_url("")).rstrip("/")
            sb._songloft_token = fake_token
            sb._songloft_cfg = lambda k, d=None: base if k == "songloft_base" else d
            ok = await cch_cast.revoke("bridge-cch", 1, "e1")
            self.assertTrue(ok, "502 should count as ticket revoked")
            self.assertEqual(bodies[0]["name"], "stop")
        finally:
            sb._songloft_token = orig_token
            sb._songloft_cfg = orig_cfg
            await client.close()


class CastSwitchTest(unittest.IsolatedAsyncioTestCase):
    """票 16/17（D 方向）：CCH 主动投送默认关闭。

    关闭的理由是设备侧实证（facts §2.27 / §2.30）：原厂 AGMC 云链与桥链并发，
    桥链 8/8 次必输；且桥的 MediaPlayer 是独立第二声源，原厂暂停后仍在出声。
    因此 target=cch 的点歌在默认配置下必须「不 prepare、不 cast、不回复」。
    """

    def setUp(self):
        sr.reset_playback_target()

    def tearDown(self):
        sr.reset_playback_target()

    def test_default_disabled(self):
        self.assertFalse(cch_cast.cast_enabled(), "默认配置必须关闭 CCH 投送")

    def test_string_values_accepted(self):
        # 面板运行时覆盖层可能给字符串，必须容忍
        orig = cch_cast._cfg
        try:
            for val, expect in (("true", True), ("1", True), ("on", True),
                                ("false", False), ("0", False), ("", False), (None, False)):
                cch_cast._cfg = lambda k, d=None, _v=val: _v
                self.assertEqual(cch_cast.cast_enabled(), expect, f"value={val!r}")
        finally:
            cch_cast._cfg = orig

    async def test_run_cch_skips_cast_when_disabled(self):
        """关闭时：cast() 一次都不调，返回空串（不出声）。"""
        calls = {"cast": 0, "prepare": 0}

        async def fake_cast(url):
            calls["cast"] += 1
            return True

        async def fake_resolve(*a, **kw):
            calls["prepare"] += 1
            return "http://ticket"

        orig_enabled = cch_cast.cast_enabled
        orig_cast, orig_resolve = cch_cast.cast, cch_cast.resolve_ticket_url
        try:
            cch_cast.cast_enabled = lambda: False
            cch_cast.cast = fake_cast
            cch_cast.resolve_ticket_url = fake_resolve
            sr.set_playback_target("cch")
            out = await sr._run_cch("play", {"query": "屋顶"})
            self.assertEqual(out, "", "关闭时不应产出任何语音回复")
            self.assertEqual(calls, {"cast": 0, "prepare": 0},
                             "关闭时不得 prepare 也不得投送")
        finally:
            cch_cast.cast_enabled = orig_enabled
            cch_cast.cast, cch_cast.resolve_ticket_url = orig_cast, orig_resolve

    async def test_run_cch_control_actions_skipped_when_disabled(self):
        """播控骨架同样静默：不回复、不动队列。"""
        orig_enabled = cch_cast.cast_enabled
        try:
            cch_cast.cast_enabled = lambda: False
            for action in ("pause", "resume", "next", "prev", "stop"):
                out = await sr._run_cch(action, {})
                self.assertEqual(out, "", f"{action} 关闭态应静默")
        finally:
            cch_cast.cast_enabled = orig_enabled


if __name__ == "__main__":
    unittest.main()
