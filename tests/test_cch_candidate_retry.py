# -*- coding: utf-8 -*-
"""CCH 点播取链失败换候选重试（错版歌手取到 404 时不哑火）。

覆盖 ``_run_cch`` 的 np + M5 轨：首个候选取链失败时按候选列表重试，
播报/renderData/队列/歌词 id 全部跟随最终选中的曲，全部失败才返回错误串。
"""

import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from core.music import cch_cast
from core.tools import song_request as sr


class _FakeSession:
    """仅支持 async with；取链失败走 fallback 时不真正发 HTTP。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class CchCandidateRetryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._state_backup = dict(sr._cch_state)

    def tearDown(self):
        sr._cch_state.clear()
        sr._cch_state.update(self._state_backup)

    def _setup(self, stack, *, items, pick, resolve, host_cmd, calls):
        """统一 mock np+M5 轨的外部依赖（不做真实网络/设备调用）。"""
        real_build = cch_cast.build_render_data

        def capture_build(url, it, meta, background_image=""):
            calls["build_item"] = it
            return real_build(url, it, meta, background_image=background_image)

        async def fake_speak(text):
            calls["speak"].append(text)
            return True, ""

        stack.enter_context(patch.object(sr, "_managed_collect", AsyncMock(return_value=items)))
        stack.enter_context(patch.object(sr, "_ensure_host_key", AsyncMock()))
        stack.enter_context(patch.object(sr, "_robust_stop", AsyncMock()))
        stack.enter_context(patch.object(sr, "_pick_url", side_effect=pick))
        stack.enter_context(patch.object(sr, "_sources", lambda req: ["kugou"]))
        stack.enter_context(patch.object(sr, "_cfg", lambda key, default=None: default))
        stack.enter_context(patch.object(sr, "_tts_wait_seconds", lambda text: 0))
        stack.enter_context(patch.object(sr, "_api", AsyncMock(return_value=None)))
        stack.enter_context(patch.object(cch_cast, "np_enabled", lambda: True))
        stack.enter_context(patch.object(cch_cast, "m5_enabled", lambda: True))
        stack.enter_context(patch.object(cch_cast, "kwqq_meta", AsyncMock(return_value={})))
        stack.enter_context(patch.object(cch_cast, "resolve_ticket_url", side_effect=resolve))
        stack.enter_context(patch.object(cch_cast, "build_render_data", side_effect=capture_build))
        stack.enter_context(patch.object(cch_cast, "host_cmd", host_cmd))
        stack.enter_context(patch.object(cch_cast, "host_cmd_nowait", AsyncMock()))
        stack.enter_context(patch.object(cch_cast, "musicctl", AsyncMock()))
        stack.enter_context(patch.object(cch_cast, "push_queue", AsyncMock()))
        stack.enter_context(patch.object(cch_cast, "speak", side_effect=fake_speak))
        stack.enter_context(patch.object(cch_cast, "bg_base", lambda: ""))
        stack.enter_context(patch("core.music.bg_cache.ensure", AsyncMock(return_value="")))
        stack.enter_context(patch("core.music.cch_click.note_recent_play", MagicMock()))
        stack.enter_context(patch("core.music.cch_history.note", MagicMock()))
        stack.enter_context(patch("core.tools.registry.suppress_reply", MagicMock()))
        stack.enter_context(patch("aiohttp.ClientSession", _FakeSession))

    async def test_raw_id_skips_search_uses_truth_source(self):
        """FAVCLICK 源头 rawID+平台 → 跳过按标题搜索，ref 用 rawID/source（§2.77-57）。
        票据链 mock 走 fallback（_FakeSession 无 get），断言点：ref/item 构造正确、搜索被跳过。"""
        calls = {"build_item": None, "speak": [], "pick": [], "resolve": []}

        async def fake_pick(http, source, it):
            calls["pick"].append(it["id"])
            return "http://good"

        async def fake_resolve(ref, *a, **kw):
            calls["resolve"].append(ref)
            return "http://ticket"

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=[], pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            # 覆盖 setUp 的 _managed_collect：一旦被调用即失败 → 证明 raw_id 分支跳过搜索
            managed = stack.enter_context(patch.object(
                sr, "_managed_collect",
                AsyncMock(side_effect=AssertionError("search must be skipped"))))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            out = await sr._run_cch("play", {
                "query": "夜来香", "raw_id": "600919000005273800", "source": "MIGU"})

        self.assertIn("已在 CCH 上为你播放", out)
        managed.assert_not_awaited()
        self.assertEqual(calls["resolve"][0]["id"], "600919000005273800")
        self.assertEqual(calls["resolve"][0]["source"], "migu")
        self.assertEqual(calls["build_item"]["id"], "600919000005273800")
        self.assertEqual(calls["build_item"]["source"], "migu", "平台角标取真实平台")

    async def test_raw_id_enriches_copyright_for_migu(self):
        """rawID 直构时从 Songloft 收藏 source_data.c 补 extra.copyrightId（§2.77-58）——
        咪咕 /song/url 需要 copyright，直构丢了搜索项的 extra 会 404（本轮真机回归）。"""
        calls = {"build_item": None, "speak": [], "pick": [], "resolve": []}
        hit = {"song_id": 99, "title": "夜来香", "cover": "", "album": "",
               "artist": "", "copyrightId": "6005663JF5A", "source": "migu"}

        async def fake_pick(http, source, it):
            calls["pick"].append(it["id"])
            return "http://good"

        async def fake_resolve(ref, *a, **kw):
            calls["resolve"].append(ref)
            return "http://ticket"

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=[], pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            stack.enter_context(patch.object(sr, "_managed_collect", AsyncMock()))
            stack.enter_context(patch("core.music.songloft_api.lookup_favorite_by_rawid",
                                      AsyncMock(return_value=hit)))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            await sr._run_cch("play", {
                "query": "夜来香", "raw_id": "600919000005273800", "source": "migu"})

        self.assertEqual(calls["build_item"]["extra"].get("copyrightId"), "6005663JF5A")
        self.assertEqual(calls["build_item"]["source"], "migu")

    async def test_raw_id_enriches_cover_album_artist(self):
        """§2.77-59：FAVCLICK rawID 直构从门面按 rawID 精确匹配补 cover/album/artist。
        否则点红心收藏时写 Songloft 的 item 无 cover → 收藏 tab 无封面（恰似你的温柔/kuwo）。"""
        calls = {"build_item": None, "speak": [], "pick": [], "resolve": []}
        # 新架构：lookup_favorite_by_rawid 单一接口反查收藏记录（无 cover）
        hit = {"song_id": 4199, "title": "恰似你的温柔", "cover": "", "album": "",
               "artist": "", "copyrightId": "", "source": "kuwo"}
        facade = [
            {"id": "671381931", "name": "恰似你的温柔", "artist": "邓丽君",
             "album": "华语经典好歌珍藏",
             "cover": "https://img4.kuwo.cn/star/albumcover/xxx.jpg", "source": "kuwo"},
            {"id": "OTHER", "name": "恰似你的温柔", "artist": "蔡琴",
             "cover": "https://wrong.jpg", "source": "kugou"},
        ]

        async def fake_pick(http, source, it):
            return "http://good"

        async def fake_resolve(ref, *a, **kw):
            return "http://ticket"

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=[], pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            stack.enter_context(patch.object(sr, "_managed_collect", AsyncMock()))
            stack.enter_context(patch("core.music.songloft_api.lookup_favorite_by_rawid",
                                      AsyncMock(return_value=hit)))
            stack.enter_context(patch("core.music.songloft_api.search",
                                      AsyncMock(return_value=facade)))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            await sr._run_cch("play", {
                "query": "恰似你的温柔", "raw_id": "671381931", "source": "kuwo"})

        it = calls["build_item"]
        self.assertIn("img4.kuwo.cn", it.get("cover", ""), "封面取门面原图（按 rawID 精确匹配）")
        self.assertEqual(it.get("album"), "华语经典好歌珍藏")
        self.assertEqual(it.get("artist"), "邓丽君")

    async def test_raw_id_cover_fail_open(self):
        """门面搜索失败 → cover 为空但不崩、点播照常（fail-open）。"""
        calls = {"build_item": None, "speak": [], "pick": [], "resolve": []}

        async def fake_pick(http, source, it):
            return "http://good"

        async def fake_resolve(ref, *a, **kw):
            return "http://ticket"

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=[], pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            stack.enter_context(patch.object(sr, "_managed_collect", AsyncMock()))
            stack.enter_context(patch("core.music.songloft_api.lookup_favorite_by_rawid",
                                      AsyncMock(return_value=None)))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            out = await sr._run_cch("play", {
                "query": "恰似你的温柔", "raw_id": "671381931", "source": "kuwo"})

        self.assertIn("已在 CCH 上为你播放", out)
        self.assertFalse(calls["build_item"].get("cover"))

    async def test_raw_id_copyright_fail_open(self):
        """Songloft 收藏读取失败 → copyright 空但不崩、点播照常（fail-open，不退化）。"""
        calls = {"build_item": None, "speak": [], "pick": [], "resolve": []}

        async def fake_pick(http, source, it):
            return "http://good"

        async def fake_resolve(ref, *a, **kw):
            return "http://ticket"

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=[], pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            stack.enter_context(patch.object(sr, "_managed_collect", AsyncMock()))
            stack.enter_context(patch("core.music.songloft_api.lookup_favorite_by_rawid",
                                      AsyncMock(side_effect=RuntimeError("songloft down"))))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            out = await sr._run_cch("play", {
                "query": "夜来香", "raw_id": "600919000005273800", "source": "migu"})

        self.assertIn("已在 CCH 上为你播放", out)
        self.assertFalse(calls["build_item"]["extra"].get("copyrightId"))

    async def test_switch_candidate_when_first_fails(self):
        """首选取链失败 → 换到可播候选：renderData/播报/队列都跟随最终曲。"""
        wrong = {"id": "WRONG", "name": "不著痕跡", "artist": "方继平", "source": "kugou"}
        right = {"id": "RIGHT", "name": "不著痕跡", "artist": "邓丽君", "source": "kugou"}
        calls = {"build_item": None, "speak": [], "pick": []}

        async def fake_pick(http, source, it):
            calls["pick"].append(it["id"])
            return "http://good" if it["id"] == "RIGHT" else None

        async def fake_resolve(*a, **kw):
            raise RuntimeError("no ticket")

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=[wrong, right], pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            out = await sr._run_cch("play", {"query": "不著痕跡"})

        self.assertIn("已在 CCH 上为你播放", out)
        host_cmd.assert_awaited_once_with("play", url="http://good")
        self.assertIs(calls["build_item"], right, "renderData 必须用最终选中的候选")
        self.assertEqual(sr._cch_state["items"][0]["id"], "RIGHT", "队列首项须是最终曲")
        self.assertIn("WRONG", calls["pick"])
        self.assertIn("RIGHT", calls["pick"])
        self.assertTrue(calls["speak"], "应有一次播报")
        self.assertIn("邓丽君", calls["speak"][-1], "重建的播报须用最终曲的歌手")

    async def test_all_candidates_fail_returns_error(self):
        """所有候选取链都失败 → 才返回原错误串，且不起播。"""
        items = [{"id": f"ID-{i}", "name": "不著痕跡", "artist": "方继平",
                  "source": "kugou"} for i in range(3)]
        calls = {"build_item": None, "speak": [], "pick": []}

        async def fake_pick(http, source, it):
            calls["pick"].append(it["id"])
            return None

        async def fake_resolve(*a, **kw):
            raise RuntimeError("no ticket")

        host_cmd = AsyncMock(return_value=(True, "OK"))
        with ExitStack() as stack:
            self._setup(stack, items=items, pick=fake_pick,
                        resolve=fake_resolve, host_cmd=host_cmd, calls=calls)
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            out = await sr._run_cch("play", {"query": "不著痕跡"})

        self.assertIn("CCH 音源暂不可用", out)
        self.assertIn("原因:", out, "失败文案须带真实原因，不能只给泛化提示")
        host_cmd.assert_not_awaited()
        self.assertIsNone(calls["build_item"])

    async def test_fallback_total_budget_caps_latency(self):
        """H1：多源串行 fallback 必须有总预算——单源慢不能累积成 50s 等待。"""
        import asyncio
        import time

        items = [{"id": "X", "name": "x", "artist": "a", "source": "qq"}]
        calls = {"build_item": None, "speak": [], "pick": []}
        tried = []

        async def fake_pick(http, source, it):
            tried.append(source)
            await asyncio.sleep(0.2)  # 模拟 kwqq 上游慢（真实日志为 15s 超时）
            return None

        async def fake_resolve(*a, **kw):
            raise RuntimeError("no ticket")

        with ExitStack() as stack:
            self._setup(stack, items=items, pick=fake_pick,
                        resolve=fake_resolve,
                        host_cmd=AsyncMock(return_value=(True, "OK")), calls=calls)
            stack.enter_context(patch.object(sr, "_sources", lambda req: ["qq", "kugou", "migu"]))
            stack.enter_context(patch.object(sr, "_CCH_FALLBACK_BUDGET_S", 0.25))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            t0 = time.monotonic()
            await sr._run_cch("play", {"query": "x"})
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 0.5, f"总耗时 {elapsed:.2f}s 应被 0.25s 预算截断")
        self.assertLess(len(tried), 3, "预算耗尽后不应继续尝试第三个源")

    async def test_prepare_budget_caps_latency(self):
        """真实日志显示 prepare 轮询单独吃 30s——必须有独立预算，超时即走 fallback。"""
        import asyncio
        import time

        items = [{"id": "X", "name": "x", "artist": "a", "source": "qq"}]
        calls = {"build_item": None, "speak": [], "pick": []}

        async def fake_resolve(*a, **kw):
            await asyncio.sleep(0.3)  # 模拟 kwqq resolve 上游慢
            return "http://ticket"

        async def fake_pick(http, source, it):
            return "http://direct"

        with ExitStack() as stack:
            self._setup(stack, items=items, pick=fake_pick,
                        resolve=fake_resolve,
                        host_cmd=AsyncMock(return_value=(True, "OK")), calls=calls)
            stack.enter_context(patch.object(sr, "_CCH_PREPARE_BUDGET_S", 0.1))
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            t0 = time.monotonic()
            out = await sr._run_cch("play", {"query": "x"})
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 0.25, f"prepare 耗时 {elapsed:.2f}s 应被 0.1s 预算截断")
        self.assertIn("已在 CCH 上为你播放", out, "prepare 超时后应走 fallback 成功")

    async def test_error_message_reports_real_reason(self):
        """H4：错误文案带真实原因，不再把上游 resolve 超时伪装成 VerifyMP3。"""
        items = [{"id": "X", "name": "x", "artist": "a", "source": "qq"}]
        calls = {"build_item": None, "speak": [], "pick": []}

        async def fake_pick(http, source, it):
            return None

        async def fake_resolve(*a, **kw):
            raise RuntimeError("kwqq resolve timeout")

        with ExitStack() as stack:
            self._setup(stack, items=items, pick=fake_pick,
                        resolve=fake_resolve,
                        host_cmd=AsyncMock(return_value=(True, "OK")), calls=calls)
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            out = await sr._run_cch("play", {"query": "x"})

        self.assertIn("CCH 音源暂不可用", out)
        self.assertIn("kwqq resolve timeout", out, "须把真实失败原因透出，避免误判为 VerifyMP3")

    async def test_candidate_cap_eight(self):
        """候选遍历上限前 8 个（13 个候选时不得全量串行尝试）。"""
        # 不同歌手版本（换候选场景）：去重键 (歌手,歌名) 保留全部 13 首
        items = [{"id": f"ID-{i}", "name": "不著痕跡", "artist": f"歌手{i}",
                  "source": "kugou"} for i in range(13)]
        calls = {"build_item": None, "speak": [], "pick": []}
        resolve_calls = {"n": 0}

        async def fake_pick(http, source, it):
            calls["pick"].append(it["id"])
            return None

        async def fake_resolve(*a, **kw):
            resolve_calls["n"] += 1
            raise RuntimeError("no ticket")

        with ExitStack() as stack:
            self._setup(stack, items=items, pick=fake_pick,
                        resolve=fake_resolve, host_cmd=AsyncMock(), calls=calls)
            sr._cch_state.update({"entry": 0, "items": [], "session": sr._CCH_SESSION})
            await sr._run_cch("play", {"query": "不著痕跡"})

        self.assertEqual(resolve_calls["n"], 8, "只应尝试前 8 个候选")
        self.assertEqual(len(set(calls["pick"])), 8)


class LyricContentTest(unittest.TestCase):
    """同平台歌词空壳判定：只有元数据行不算歌词，须触发跨源回搜。"""

    def test_netease_empty_shell_rejected(self):
        shell = '{"t":-1,"c":[{"tx":"作词: "},{"tx":"Unknown"}]}\n' \
                 '{"t":-1,"c":[{"tx":"作曲: "},{"tx":"松井五郎"}]}'
        self.assertFalse(sr._lyric_has_content(shell))

    def test_meta_only_rejected(self):
        self.assertFalse(sr._lyric_has_content(
            "[ti:不着痕迹]\n[ar:邓丽君]\n[al:偿还]\n[offset:0]\n[by:]\n"))

    def test_timed_lyrics_accepted(self):
        lrc = ("[ti:不着痕迹]\n[ar:邓丽君]\n"
               "[00:09.280]词：洪光达\n[00:27.858]在感情结束的时候\n"
               "[00:31.000]你会不会想起我")
        self.assertTrue(sr._lyric_has_content(lrc))

    def test_empty_timestamp_line_rejected(self):
        self.assertFalse(sr._lyric_has_content("[00:00.000]\n[00:05.000]"))

    def test_plain_untimed_text_rejected(self):
        self.assertFalse(sr._lyric_has_content("作词: Unknown\n作曲: 松井五郎"))


class PickUrlFormatTest(unittest.IsolatedAsyncioTestCase):
    """取链格式：2026-11-24 勘误后 m4a 不再拒收（Apollo 支持，gate 已放行 QQ 域名）；
    预览/ekey 仍按既有判据拒收。"""

    async def _pick(self, url):
        async def fake_api(session, path, params):
            return {"url": url, "duration_s": 240, "size_bytes": 5_000_000}
        with patch.object(sr, "_api", fake_api):
            return await sr._pick_url(_FakeSession(), "qq", {"id": "X", "source": "kugou"})

    async def test_m4a_accepted(self):
        url = "https://isure6.stream.qqmusic.qq.com/C600002XrpfT0qvNMt.m4a?guid=1"
        self.assertEqual(await self._pick(url), url)

    async def test_mp3_accepted(self):
        self.assertEqual(await self._pick(
            "http://fs.kugou.com/x/yp/full/song.mp3?t=1"),
            "http://fs.kugou.com/x/yp/full/song.mp3?t=1")


if __name__ == "__main__":
    unittest.main()
