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
