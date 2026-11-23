# -*- coding: utf-8 -*-
"""CCH 点播取链失败换候选重试（错版歌手取到 404 时不哑火）。

覆盖 ``_run_cch`` 的 np + M5 轨：首个候选取链失败时按候选列表重试，
播报/renderData/队列/歌词 id 全部跟随最终选中的曲，全部失败才返回错误串。
"""

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

        self.assertEqual(out, "CCH 音源暂不可用（kwqq 解析冷却/预览降级），请稍后再试或换音源")
        host_cmd.assert_not_awaited()
        self.assertIsNone(calls["build_item"])

    async def test_candidate_cap_eight(self):
        """候选遍历上限前 8 个（13 个候选时不得全量串行尝试）。"""
        items = [{"id": f"ID-{i}", "name": "不著痕跡", "artist": "方继平",
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
    """CCH Apollo 兼容：拒收 m4a/mp4 直链（error state:2 无声），收 mp3。"""

    async def _pick(self, url):
        async def fake_api(session, path, params):
            return {"url": url, "duration_s": 240, "size_bytes": 5_000_000}
        with patch.object(sr, "_api", fake_api):
            return await sr._pick_url(_FakeSession(), "qq", {"id": "X", "source": "kugou"})

    async def test_m4a_rejected(self):
        self.assertIsNone(await self._pick(
            "https://isure6.stream.qqmusic.qq.com/C600002XrpfT0qvNMt.m4a?guid=1"))

    async def test_mp3_accepted(self):
        self.assertEqual(await self._pick(
            "http://fs.kugou.com/x/yp/full/song.mp3?t=1"),
            "http://fs.kugou.com/x/yp/full/song.mp3?t=1")


if __name__ == "__main__":
    unittest.main()
