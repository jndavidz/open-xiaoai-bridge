"""Songloft 托管准备链解析器（B-08）契约测试。

纯内存 aiohttp TestServer 伪造 Songloft：登录缓存、prepare 202→轮询
ready→票据绝对 URL；failed → ManagedError；凭证缺失 → 明确失败。
不连设备、不联网。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from core.music.model import ManagedError, TrackRef  # noqa: E402
from core.music.player import QueueEntry  # noqa: E402
from core.music import song_bridge  # noqa: E402


def _entry(entry_id="e1"):
    return QueueEntry(entry_id=entry_id,
                      ref=TrackRef.make("kuwo", "228908", {}))


class SongloftResolverTest(unittest.IsolatedAsyncioTestCase):
    async def test_ready_chain_returns_ticket_url(self):
        logins = []

        async def login(req):
            logins.append(1)
            return web.json_response({"access_token": f"jwt-{len(logins)}"})

        async def prepare(req):
            return web.json_response({}, status=202)

        async def poll(req):
            return web.json_response({
                "state": "ready",
                "media_url": "/media?media_ticket=abc",
                "resolved_source": "kuwo",
                "duration_s": 236.5,
            })

        app = web.Application()
        app.router.add_post("/api/v1/auth/login", login)
        app.router.add_post("/api/v1/managed/prepare", prepare)
        app.router.add_get("/api/v1/managed/prepare", poll)
        srv = TestServer(app)
        client = TestClient(srv)
        await client.start_server()
        try:
            base = str(srv.make_url("")).rstrip("/")
            song_bridge._songloft_token_cache.clear()
            # 配置注入
            import core.tools.song_request as sr
            orig = sr.ConfigManager.instance().get_app_config
            sr.ConfigManager.instance().get_app_config = (
                lambda key, default=None: {
                    "song_request.songloft_base": base,
                    "song_request.songloft_user": "admin",
                    "song_request.songloft_password": "pw",
                }.get(key, default))

            url, duration = await song_bridge._songloft_resolver(_entry())
            self.assertEqual(url, base + "/media?media_ticket=abc")
            self.assertAlmostEqual(duration, 236.5)
            self.assertEqual(len(logins), 1)
            # 第二次调用命中 token 缓存
            await song_bridge._songloft_resolver(_entry("e2"))
            self.assertEqual(len(logins), 1)
        finally:
            sr.ConfigManager.instance().get_app_config = orig
            song_bridge._songloft_token_cache.clear()
            await client.close()

    async def test_failed_prepare_raises(self):
        async def login(req):
            return web.json_response({"access_token": "jwt"})

        async def prepare(req):
            return web.json_response({}, status=202)

        async def poll(req):
            return web.json_response({"state": "failed",
                                      "error": "verify: not mp3"})

        app = web.Application()
        app.router.add_post("/api/v1/auth/login", login)
        app.router.add_post("/api/v1/managed/prepare", prepare)
        app.router.add_get("/api/v1/managed/prepare", poll)
        srv = TestServer(app)
        client = TestClient(srv)
        await client.start_server()
        try:
            base = str(srv.make_url("")).rstrip("/")
            song_bridge._songloft_token_cache.clear()
            import core.tools.song_request as sr
            orig = sr.ConfigManager.instance().get_app_config
            sr.ConfigManager.instance().get_app_config = (
                lambda key, default=None: {
                    "song_request.songloft_base": base,
                    "song_request.songloft_password": "pw",
                }.get(key, default))
            with self.assertRaises(ManagedError) as ctx:
                await song_bridge._songloft_resolver(_entry())
            self.assertIn("not mp3", str(ctx.exception))
        finally:
            sr.ConfigManager.instance().get_app_config = orig
            song_bridge._songloft_token_cache.clear()
            await client.close()

    async def test_missing_password_fails_closed(self):
        song_bridge._songloft_token_cache.clear()
        import core.tools.song_request as sr
        orig = sr.ConfigManager.instance().get_app_config
        sr.ConfigManager.instance().get_app_config = (
            lambda key, default=None: {
                "song_request.songloft_base": "http://127.0.0.1:1",
                "song_request.songloft_password": "",
            }.get(key, default))
        try:
            with self.assertRaises(ManagedError):
                await song_bridge._songloft_resolver(_entry())
        finally:
            sr.ConfigManager.instance().get_app_config = orig
            song_bridge._songloft_token_cache.clear()


if __name__ == "__main__":
    unittest.main()
