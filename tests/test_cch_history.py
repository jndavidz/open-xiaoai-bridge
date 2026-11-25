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


if __name__ == "__main__":
    unittest.main()
