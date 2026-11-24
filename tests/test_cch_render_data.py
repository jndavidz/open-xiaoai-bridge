"""renderData 字段契约：全屏背景必须跟随本曲封面（2026-11-21「背景一直是李健」回归）。"""
from __future__ import annotations

import unittest

from core.music import cch_cast


class RenderDataTest(unittest.TestCase):
    def test_background_image_follows_cover(self):
        rd = cch_cast.build_render_data(
            "http://ticket/1.mp3",
            {"name": "十年", "artist": "陈奕迅", "cover": "http://img/shi.jpg"},
            {},
        )
        self.assertEqual("http://img/shi.jpg", rd["audioImageUrl"])
        self.assertEqual("http://img/shi.jpg", rd["backgroundImage"])
        self.assertEqual("", rd["lyricbackGroundAuhor"])
        self.assertEqual({"img": "http://img/shi.jpg"}, rd["cover"])

    def test_meta_cover_wins(self):
        rd = cch_cast.build_render_data(
            "http://ticket/1.mp3",
            {"name": "十年", "cover": "http://item.jpg"},
            {"cover": "http://meta.jpg"},
        )
        self.assertEqual("http://meta.jpg", rd["backgroundImage"])

    def test_audio_length_uses_duration_s_from_kwqq_meta(self):
        """§2.77-49：kwqq-api /song/info 返回 duration_s（秒），不是 duration——
        build_render_data 必须读 duration_s 兜底，否则 audioLength 写死 0、屏上不显示时长。"""
        rd = cch_cast.build_render_data(
            "http://ticket/1.mp3", {"name": "发如雪"},
            {"duration_s": 299},
        )
        self.assertEqual(299, rd["audioLength"])

    def test_audio_length_duration_field_still_wins(self):
        """旧调用方若仍带 duration 字段（同样按秒处理），优先级不变。"""
        rd = cch_cast.build_render_data(
            "http://ticket/1.mp3", {"name": "发如雪"},
            {"duration": 269, "duration_s": 299},
        )
        self.assertEqual(269, rd["audioLength"])

    def test_audio_length_item_duration_s_fallback(self):
        """meta 空时从 item.duration_s 兜底（点播队列项发票的时长）。"""
        rd = cch_cast.build_render_data(
            "http://ticket/1.mp3", {"name": "发如雪", "duration_s": 240}, {},
        )
        self.assertEqual(240, rd["audioLength"])

    def test_audio_length_edge_values(self):
        """§2.77-49 防御：字符串浮点/负数/非法值不抛异常、不写出负 audioLength。"""
        cases = [
            ({"duration_s": "299.5"}, 299),   # 字符串浮点 → 截断
            ({"duration_s": -5}, 0),           # 负数 → 钳到 0
            ({"duration_s": "bad"}, 0),        # 非法 → 0
            ({"duration_s": None}, 0),         # None → 0
        ]
        for meta, expect in cases:
            with self.subTest(meta=meta):
                rd = cch_cast.build_render_data("http://t/1.mp3", {"name": "x"}, meta)
                self.assertEqual(expect, rd["audioLength"])


if __name__ == "__main__":
    unittest.main()


class BgCacheKeyTest(unittest.TestCase):
    def test_key_is_stable_and_url_safe(self):
        from core.music import bg_cache

        k = bg_cache.key_for("http://img/a.jpg")
        self.assertEqual(16, len(k))
        self.assertTrue(all(c in "0123456789abcdef" for c in k))
        self.assertEqual(k, bg_cache.key_for("http://img/a.jpg"))
        self.assertNotEqual(k, bg_cache.key_for("http://img/b.jpg"))
        self.assertTrue(bg_cache.file_path(k).endswith(k + ".jpg"))


class RenderDataBackgroundTest(unittest.TestCase):
    def test_explicit_background_wins(self):
        from core.music import cch_cast

        rd = cch_cast.build_render_data(
            "http://t/1.mp3", {"name": "十年", "cover": "http://img/c.jpg"}, {},
            background_image="http://10.10.10.2:9092/api/cch/bg/abc.jpg")
        self.assertEqual("http://10.10.10.2:9092/api/cch/bg/abc.jpg", rd["backgroundImage"])
        self.assertEqual("http://img/c.jpg", rd["audioImageUrl"])
