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
