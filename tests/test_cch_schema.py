# -*- coding: utf-8 -*-
"""CCH 原厂 item schema 公共函数测试（TDD seam：输出 schema 的 observable behavior）。

两个 tab（收藏 / 播放历史）共用同一 schema 生成器；本测试用已知字面量断言关键
字段，不 mock 内部、不复述实现逻辑。字段依据 facts §2.76-4 原厂 V2 响应。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.music import cch_schema  # noqa: E402


class NativeItemSchemaTest(unittest.TestCase):
    """native_item_schema 输出字段——通过 public seam 验证。

    RED 阶段：cch_schema 尚不存在 → import 失败即 red；实现后应 green。
    """

    def test_favorite_shape_fields(self):
        item = cch_schema.native_item_schema(
            sid=4086, raw_id="8C655F57", source="kugou", title="胡广生",
            artist="任素汐", album="胡广生", cover="http://img.example/c.jpg",
            duration=214, pos=0, collect=True, play_from="playFavorite",
            display_title="胡广生-《胡广生》-酷狗", include_album_item=True)
        # 唯一身份与展示：独立字面量，不重算
        self.assertEqual("8C655F57", item["rawId"])
        self.assertEqual("KUGOU", item["source"])
        self.assertEqual("胡广生-《胡广生》-酷狗", item["title"])
        self.assertEqual("playFavorite", item["playFrom"])
        self.assertTrue(item["collect"])
        self.assertEqual(4086, item["id"])
        # 封面壳：img 必填 + 原厂固定字段
        self.assertEqual("http://img.example/c.jpg", item["cover"]["img"])
        self.assertIsNone(item["cover"]["large"])
        # authors 带歌手
        self.assertEqual("任素汐", item["authors"][0]["title"])
        self.assertEqual("SINGER", item["authors"][0]["authorTypes"][0]["name"])
        # 收藏 tab 专属 albumItem 存在
        self.assertIsNotNone(item["albumItem"])
        self.assertEqual("胡广生", item["albumItem"]["title"])

    def test_history_shape_fields(self):
        item = cch_schema.native_item_schema(
            sid=0, raw_id="347BDED5", source="kugou", title="屋顶",
            artist="温岚", album="爱回转", cover="http://img.example/r.jpg",
            duration=319, pos=2, collect=False, play_from="playHistory",
            history_extras=True)
        self.assertEqual("347BDED5", item["rawId"])
        self.assertEqual("屋顶", item["title"])
        self.assertEqual("playHistory", item["playFrom"])
        self.assertEqual(0, item["id"])
        self.assertFalse(item["collect"])
        # 历史 tab 专属扩展字段
        self.assertEqual("content-audio", item["firstType"])
        self.assertEqual("music", item["secondType"])
        self.assertNotIn("albumItem", item)  # 历史 tab 无 albumItem（收藏 tab 才有）
        # screenPlayUrl 用固定深链 + (rawId, source)，无票据
        self.assertIn('"rawId":"347BDED5"', item["screenPlayUrl"])
        self.assertIn('"source":"KUGOU"', item["screenPlayUrl"])
        self.assertNotIn("ticket", item["screenPlayUrl"])

    def test_no_artist_yields_empty_authors(self):
        item = cch_schema.native_item_schema(
            sid=1, raw_id="X", source="kwqq", title="未知", artist="", album="",
            cover="", duration=0, pos=0, collect=False, play_from="playHistory")
        self.assertEqual([], item["authors"])
        self.assertEqual("", item["cover"]["img"])


if __name__ == "__main__":
    unittest.main()
