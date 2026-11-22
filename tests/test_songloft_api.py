"""Songloft 统一入口的契约测试（票 24）：结果映射 + 聚合搜索/跨源歌词的调用形状。

不联网：`_request` 被替换为桩，验证 bridge 侧的归一与降级契约。
"""
from __future__ import annotations

import json
import unittest

from core.music import songloft_api


class ToItemTest(unittest.TestCase):
    def test_maps_plugin_result_to_facade_item(self):
        item = songloft_api.to_item({
            "title": "晴天",
            "artist": "周杰伦",
            "album": "叶惠美",
            "duration": 269,
            "cover_url": "http://img/1.jpg",
            "source_data": {"s": "kugou", "i": "HASH1", "n": "晴天", "a": "周杰伦"},
        })
        self.assertEqual("晴天", item["name"])
        self.assertEqual("周杰伦", item["artist"])
        self.assertEqual("kugou", item["source"])
        self.assertEqual("HASH1", item["id"])
        self.assertEqual(269.0, item["duration_s"])
        self.assertEqual({}, item["extra"])

    def test_maps_copyright_for_migu(self):
        item = songloft_api.to_item({
            "title": "x", "artist": "y",
            "source_data": {"s": "migu", "i": "C1", "c": "CR9"},
        })
        self.assertEqual({"copyrightId": "CR9"}, item["extra"])


class _AsyncStub:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def __call__(self, http, method, path, *, params=None, json_body=None):
        self.calls.append((method, path, params, json_body))
        return self.payload


class SearchLyricContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_normalizes_and_drops_incomplete(self):
        stub = _AsyncStub({"results": [
            {"title": "晴天", "artist": "周杰伦", "source_data": {"s": "kugou", "i": "H1"}},
            {"title": "无源", "artist": "x", "source_data": {}},
        ]})
        orig = songloft_api._request
        songloft_api._request = stub
        try:
            items = await songloft_api.search("晴天", http=object())
        finally:
            songloft_api._request = orig
        self.assertEqual(1, len(items))
        self.assertEqual("kugou", items[0]["source"])
        method, path, _, body = stub.calls[0]
        self.assertEqual("POST", method)
        self.assertTrue(path.endswith("/api/search"))
        self.assertEqual("晴天", body["keyword"])

    async def test_lyric_returns_lxlyric_passthrough(self):
        stub = _AsyncStub({"lyric": "[00:01]a", "lxlyric": "[00:01]<0,500>a"})
        orig = songloft_api._request
        songloft_api._request = stub
        try:
            ly = await songloft_api.lyric("晴天", "周杰伦", http=object())
        finally:
            songloft_api._request = orig
        self.assertEqual("[00:01]<0,500>a", ly["lxlyric"])
        _, path, params, _ = stub.calls[0]
        self.assertTrue(path.endswith("/lyric-search"))
        self.assertEqual("晴天", params["title"])
        self.assertEqual("周杰伦", params["artist"])

    async def test_lyric_empty_title_short_circuits(self):
        stub = _AsyncStub({})
        orig = songloft_api._request
        songloft_api._request = stub
        try:
            self.assertEqual({}, await songloft_api.lyric("", http=object()))
        finally:
            songloft_api._request = orig
        self.assertEqual([], stub.calls)


if __name__ == "__main__":
    unittest.main()


class PickLrcTest(unittest.TestCase):
    def test_prefers_line_level_lyric(self):
        self.assertEqual("[00:01]a", songloft_api.pick_lrc(
            {"lyric": "[00:01]a", "lxlyric": "[00:01]<0,500>a"}))

    def test_falls_back_to_word_level_stripped(self):
        self.assertEqual("[00:01]ab\n[00:02]cd", songloft_api.pick_lrc(
            {"lyric": "", "lxlyric": "[00:01]<0,500>a<500,500>b\n[00:02]<0,500>c<500,500>d"}))

    def test_empty_payload(self):
        self.assertEqual("", songloft_api.pick_lrc({}))
        self.assertEqual("", songloft_api.pick_lrc({"lyric": "  ", "lxlyric": ""}))


class FavoritesContractTest(unittest.IsolatedAsyncioTestCase):
    """票 13：收藏落 Songloft 内置歌单（插件引用入库 → 入单/出单）。"""

    def setUp(self):
        songloft_api._FAV_PLAYLIST.clear()

    async def test_ensure_song_posts_plugin_reference(self):
        stub = _AsyncStub({"songs": [{"id": 42}]})
        orig = songloft_api._request
        songloft_api._request = stub
        try:
            song_id = await songloft_api.ensure_song(
                {"name": "青花瓷", "artist": "周杰伦", "album": "我很忙",
                 "source": "kugou", "id": "H1", "duration_s": 239.0, "cover": "http://c"},
                http=object())
        finally:
            songloft_api._request = orig
        self.assertEqual(42, song_id)
        method, path, _, body = stub.calls[0]
        self.assertEqual("POST", method)
        self.assertTrue(path.endswith("/api/v1/songs/remote"))
        rec = body[0]
        self.assertEqual("kwqq", rec["plugin_entry_path"])
        self.assertEqual("kwqq_kugou_H1", rec["dedup_key"])
        self.assertIn(rec.get("url"), (None, ""))
        self.assertIn('"s":"kugou"', rec["source_data"])

    async def test_favorites_playlist_picks_built_in_normal(self):
        stub = _AsyncStub({"playlists": [
            {"id": 3, "type": "radio", "labels": ["built_in"]},
            {"id": 1, "type": "normal", "labels": ["built_in"]},
        ]})
        orig = songloft_api._request
        songloft_api._request = stub
        try:
            pl = await songloft_api.favorites_playlist_id(http=object())
        finally:
            songloft_api._request = orig
        self.assertEqual(1, pl)
        _, path, params, _ = stub.calls[0]
        self.assertTrue(path.endswith("/api/v1/playlists"))
        self.assertEqual("normal", params["type"])

    async def test_add_and_remove_hit_playlist_members(self):
        calls = []

        async def fake_request(http, method, path, *, params=None, json_body=None):
            calls.append((method, path, json_body))
            if path.endswith("/api/v1/playlists"):
                return {"playlists": [{"id": 1, "type": "normal", "labels": ["built_in"]}]}
            return {"added": 1, "skipped": 0, "message": "ok"}

        orig = songloft_api._request
        songloft_api._request = fake_request
        try:
            await songloft_api.add_to_favorites(42, http=object())
            await songloft_api.remove_from_favorites(42, http=object())
        finally:
            songloft_api._request = orig
        self.assertEqual(("POST", "/api/v1/playlists/1/songs", {"song_ids": [42]}), calls[1])
        self.assertEqual(("DELETE", "/api/v1/playlists/1/songs/42", None), calls[2])


class WritablePlaylistTest(unittest.IsolatedAsyncioTestCase):
    """票 13 增强：playlist_id 入参的歌单可写性校验 + 指定歌单 add/remove。"""

    async def test_writable_ids_exclude_radio(self):
        async def fake_request(http, method, path, *, params=None, json_body=None):
            return {"playlists": [
                {"id": 1, "type": "normal", "labels": ["built_in"], "name": "收藏"},
                {"id": 2, "type": "normal", "name": "周末"},
                {"id": 3, "type": "radio", "name": "电台收藏"},
            ]}

        orig = songloft_api._request
        songloft_api._request = fake_request
        try:
            got = await songloft_api.writable_playlist_ids(http=object())
        finally:
            songloft_api._request = orig
        self.assertEqual({1: "收藏", 2: "周末"}, got)
        self.assertNotIn(3, got, "radio 类型不可写")

    async def test_writable_ids_empty_raises(self):
        async def fake_request(http, method, path, *, params=None, json_body=None):
            return {"playlists": [{"id": 3, "type": "radio"}]}

        orig = songloft_api._request
        songloft_api._request = fake_request
        try:
            with self.assertRaises(songloft_api.SongloftError):
                await songloft_api.writable_playlist_ids(http=object())
        finally:
            songloft_api._request = orig

    async def test_add_remove_honor_explicit_playlist_id(self):
        calls = []

        async def fake_request(http, method, path, *, params=None, json_body=None):
            calls.append((method, path))
            return {"added": 1, "message": "ok"}

        orig = songloft_api._request
        songloft_api._request = fake_request
        try:
            await songloft_api.add_to_favorites(7, http=object(), playlist_id=2)
            await songloft_api.remove_from_favorites(7, http=object(), playlist_id=2)
        finally:
            songloft_api._request = orig
        self.assertEqual(("POST", "/api/v1/playlists/2/songs"), calls[0])
        self.assertEqual(("DELETE", "/api/v1/playlists/2/songs/7"), calls[1])


class QueuePayloadTest(unittest.TestCase):
    """票 21：门面统一构造 UI 队列载荷（脱敏 / 门面 source 平台标识 / topN / 边界）。"""

    def _item(self, name, sid, source="kuwo", **kw):
        d = {"name": name, "id": sid, "source": source, "artist": "歌手",
             "album": "专辑", "cover": "http://img/x.jpg", "duration_s": 200.0}
        d.update(kw)
        return d

    def test_fields_no_ticket_and_facade_source(self):
        raw = songloft_api.queue_payload([self._item("晴天", "94237", source="kugou")])
        row = json.loads(raw)[0]
        self.assertEqual("晴天", row["name"])
        self.assertEqual("94237", row["id"])
        self.assertEqual("kugou", row["source"])  # 门面 source_data.s 原样，不加工
        self.assertEqual("歌手", row["artist"])
        self.assertEqual("专辑", row["album"])
        self.assertEqual(200, row["duration_s"])
        self.assertIn("cover", row)
        self.assertNotIn("audioUrl", raw)
        self.assertNotIn("media_ticket", raw)   # 票据不进队列（30min TTL 会过期）

    def test_singer_alias_and_missing_id_skipped(self):
        arr = json.loads(songloft_api.queue_payload([
            {"name": "A", "id": "1", "singer": "王力宏", "source": "kuwo"},
            {"name": "无ID"}, {"id": "2"},
            {"name": "B", "id": "3", "artist": "李健", "source": "netease"},
        ]))
        self.assertEqual(["A", "B"], [r["name"] for r in arr])
        self.assertEqual("王力宏", arr[0]["artist"])
        self.assertEqual("李健", arr[1]["artist"])
        self.assertEqual("netease", arr[1]["source"])

    def test_invalid_duration_and_empty(self):
        arr = json.loads(songloft_api.queue_payload(
            [self._item("x", "1", duration_s="bad")]))
        self.assertEqual(0, arr[0]["duration_s"])
        self.assertEqual("[]", songloft_api.queue_payload([]))
        self.assertEqual("[]", songloft_api.queue_payload(None))

    def test_topn_from_facade_config_and_bounds(self):
        from core.utils.config import ConfigManager

        orig = ConfigManager.instance().get_app_config
        try:
            ConfigManager.instance().get_app_config = lambda key, default: 3
            arr = json.loads(songloft_api.queue_payload(
                [self._item(f"s{i}", str(i)) for i in range(10)]))
            self.assertEqual(3, len(arr), "topN 读门面配置 song_request.cch_queue_topn")
            ConfigManager.instance().get_app_config = lambda key, default: 999
            self.assertEqual(100, songloft_api.queue_topn(), "上限钳制 100")
            ConfigManager.instance().get_app_config = lambda key, default: -1
            self.assertEqual(1, songloft_api.queue_topn(), "下限钳制 1")
            ConfigManager.instance().get_app_config = lambda key, default: "bad"
            self.assertEqual(20, songloft_api.queue_topn(), "非法值回落缺省 20")
        finally:
            ConfigManager.instance().get_app_config = orig
