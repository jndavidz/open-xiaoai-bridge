"""Songloft 统一入口的契约测试（票 24）：结果映射 + 聚合搜索/跨源歌词的调用形状。

不联网：`_request` 被替换为桩，验证 bridge 侧的归一与降级契约。
"""
from __future__ import annotations

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
