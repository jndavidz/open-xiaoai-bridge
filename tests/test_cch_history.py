# -*- coding: utf-8 -*-
"""CCH 历史真相源反查单测：按显示 pos 取 (rawID, source, name) 唯一身份。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.music import cch_history  # noqa: E402


class CchHistoryIdentityTest(unittest.TestCase):
    ITEMS = [
        {"title": "屋顶", "rawId": "347BDED5", "source": "KUGOU",
         "authors": [{"title": "温岚"}]},
        {"title": "亲爱的你啊", "rawId": "8C655F57", "source": "KUGOU",
         "authors": [{"title": "任素汐"}]},
        {"title": "夜来香", "rawId": "60091900", "source": "MIGU",
         "authors": [{"title": "邓丽君"}]},
    ]

    def test_pos_to_identity(self):
        hit = cch_history.get_by_pos_from_items(self.ITEMS, 1)
        self.assertEqual("8C655F57", hit["rawId"])
        self.assertEqual("kugou", hit["source"])
        self.assertEqual("亲爱的你啊", hit["name"])
        self.assertEqual("任素汐", hit["artist"])

    def test_pos_out_of_range(self):
        self.assertIsNone(cch_history.get_by_pos_from_items(self.ITEMS, 99))
        self.assertIsNone(cch_history.get_by_pos_from_items(self.ITEMS, -1))

    def test_bad_pos(self):
        self.assertIsNone(cch_history.get_by_pos_from_items(self.ITEMS, "abc"))

    def test_missing_identity_rejects(self):
        bad = [{"title": "无身份", "source": ""}]
        self.assertIsNone(cch_history.get_by_pos_from_items(bad, 0))

    def test_string_pos(self):
        hit = cch_history.get_by_pos_from_items(self.ITEMS, "2")
        self.assertEqual("60091900", hit["rawId"])

    def test_get_by_rawid_from_items(self):
        """票01：FAVADD 从历史真相源按 rawID 反查完整 item（供 ensure_song 重建收藏）。"""
        items = [
            {"rawId": "347BDED5", "source": "KUGOU", "title": "屋顶",
             "authors": [{"title": "温岚"}], "duration": 319,
             "cover": {"img": "http://img/r.jpg"}},
            {"rawId": "8C655F57", "source": "KUGOU", "title": "亲爱的你啊",
             "authors": [{"title": "任素汐"}]},
        ]
        hit = cch_history.get_by_rawid_from_items(items, "8C655F57")
        self.assertEqual("8C655F57", hit["rawId"])
        self.assertEqual("亲爱的你啊", hit["title"])
        self.assertEqual("温岚", items[0]["authors"][0]["title"])  # 反查返回原始 item
        self.assertEqual("任素汐", hit["authors"][0]["title"])
        self.assertNotIn("cover", hit)  # 该 item 无 cover

        # 还原成 ensure_song 格式（票 01 FAVADD 用）——用带 cover 的首项
        with_cover = cch_history.get_by_rawid_from_items(items, "347BDED5")
        fav_item = cch_history.item_for_favorite_from_history(with_cover)
        self.assertEqual("347BDED5", fav_item["id"])
        self.assertEqual("屋顶", fav_item["name"])
        self.assertEqual("kugou", fav_item["source"])
        self.assertEqual("温岚", fav_item["artist"])
        self.assertEqual("http://img/r.jpg", fav_item["cover"])
        self.assertEqual(319, fav_item["duration_s"])

        # §2.77-58：extra.copyrightId 透传（咪咕 by-id 取链所需）
        with_cp = {"rawId": "MIGU1", "source": "migu", "title": "夜来香",
                   "extra": {"copyrightId": "MIGU-CP-789"}}
        cp_item = cch_history.item_for_favorite_from_history(with_cp)
        self.assertEqual("MIGU-CP-789", cp_item["extra"]["copyrightId"])
        no_cp = cch_history.item_for_favorite_from_history({"rawId": "X", "source": "kwqq", "title": "Y"})
        self.assertNotIn("extra", no_cp)

    def test_get_by_rawid_case_insensitive_and_missing(self):
        items = [{"rawId": "ABC123", "source": "KUGOU", "title": "X"}]
        self.assertEqual("ABC123", cch_history.get_by_rawid_from_items(items, "abc123")["rawId"])
        self.assertIsNone(cch_history.get_by_rawid_from_items(items, "NOPE"))

    def test_history_schema_collect_reflects_favorites(self):
        """票01h：历史红心必须反映收藏态——按收藏 rawID 集合校准 collect。"""
        items = [
            {"name": "晴天", "id": "B3A52A7A", "source": "kugou", "artist": "周杰伦"},
            {"name": "荒原", "id": "C9A56582", "source": "kugou", "artist": "任素汐"},
        ]
        fav_rids = {"B3A52A7A"}  # 只有晴天已收藏
        out = cch_history.history_item_schema(items, fav_rids=fav_rids)
        self.assertTrue(out[0]["collect"], "晴天在收藏表 → collect=True")
        self.assertFalse(out[1]["collect"], "荒原不在收藏表 → collect=False")

    def test_history_schema_collect_case_insensitive_rawid(self):
        items = [{"name": "晴天", "id": "B3A52A7A", "source": "kugou"}]
        out = cch_history.history_item_schema(items, fav_rids={"b3a52a7a"})
        self.assertTrue(out[0]["collect"], "收藏 rawID 大小写不敏感")

    def test_history_schema_collect_default_legacy(self):
        """向后兼容：不传 fav_rids 时保留 item 自带 collect（旧行为）。"""
        items = [{"name": "X", "id": "ID1", "source": "kugou", "collect": True}]
        out = cch_history.history_item_schema(items)
        self.assertTrue(out[0]["collect"])

    def test_history_schema_collect_flips_to_false_when_unfavorited(self):
        """验收①：取消收藏后（rawID 不在集合）collect 从 True 翻转为 False。"""
        items = [{"name": "晴天", "id": "B3A52A7A", "source": "kugou", "collect": True}]
        # 收藏表已无该曲（空集合）→ collect=False
        out = cch_history.history_item_schema(items, fav_rids=set())
        self.assertFalse(out[0]["collect"], "取消收藏后红心应变空心")

    def test_load_favorite_rawids_keeps_last_cache_on_read_failure(self):
        """Spec review：Songloft 读失败时保留上次成功集合，不假阴性全灭红心。"""
        import asyncio
        from unittest import mock

        prev = cch_history._last_fav_rids
        try:
            cch_history._last_fav_rids = {"b3a52a7a"}
            with mock.patch("core.music.songloft_api.favorites_playlist_id",
                            new_callable=mock.AsyncMock, side_effect=RuntimeError("down")):
                got = asyncio.run(cch_history.load_favorite_rawids())
            self.assertEqual({"b3a52a7a"}, got, "读失败应返回上次缓存而非空集")
        finally:
            cch_history._last_fav_rids = prev

    def test_history_item_schema_via_public_seam(self):
        """history schema 输出经公共 cch_schema 生成，关键字段保持原厂形态。"""
        item = cch_history.history_item_schema([{
            "name": "屋顶", "id": "347BDED5", "source": "kugou", "artist": "温岚",
            "album": "爱回转", "cover": "http://img.example/r.jpg", "duration_s": 319,
        }])[0]
        self.assertEqual("屋顶", item["title"])
        self.assertEqual("347BDED5", item["rawId"])
        self.assertEqual("playHistory", item["playFrom"])
        self.assertEqual("content-audio", item["firstType"])
        self.assertEqual(0, item["id"])
        # spec review 守门：历史 tab isAudition=False（非 None），字段不漂移
        self.assertIs(False, item["isAudition"])


class CchHistoryPushTest(unittest.TestCase):
    """票 04（F7）：历史镜像首次读取空值与并发全量写。"""

    def test_push_with_unknown_favorites_still_writes(self):
        """首次收藏读失败（fav=None）时全量写仍完成，不抛 TypeError。"""
        import asyncio
        from unittest import mock

        async def run():
            with mock.patch.object(cch_history, "load_favorite_rawids",
                                   new_callable=mock.AsyncMock, return_value=None), \
                 mock.patch("core.music.cch_cast.musicctl",
                            new_callable=mock.AsyncMock,
                            return_value=(True, {})) as m:
                await cch_history._push()
                self.assertEqual(1, m.call_count, "未知收藏态不应阻断设备镜像写入")

        asyncio.run(run())

    def test_concurrent_push_serializes_device_writes(self):
        """并发全量写不重叠：设备镜像只反映最后一次真相源状态。"""
        import asyncio
        from unittest import mock

        async def run():
            active = 0
            peak = 0
            writes = []

            async def fake_musicctl(op, payload):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                writes.append(payload.get("items"))
                active -= 1
                return True, {}

            with mock.patch.object(cch_history, "load_favorite_rawids",
                                   new_callable=mock.AsyncMock, return_value=set()), \
                 mock.patch("core.music.cch_cast.musicctl", new=fake_musicctl):
                await asyncio.gather(cch_history._push(), cch_history._push())
            self.assertEqual(1, peak, "并发 _push 的设备全量写必须串行")
            self.assertEqual(2, len(writes))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
