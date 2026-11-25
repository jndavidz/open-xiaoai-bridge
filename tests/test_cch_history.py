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


if __name__ == "__main__":
    unittest.main()
