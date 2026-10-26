"""「换个话题」暗号识别纯函数测试（spec topic-switch-and-memory 票 02）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils.new_topic import (  # noqa: E402
    edit_distance,
    match_new_topic,
    strip_punctuation,
)

EXACT = ["我们换个话题"]
FUZZY = ["换个话题", "聊点别的", "换个话题吧"]


class StripPunctuationTest(unittest.TestCase):
    def test_strips_common_punctuation_and_whitespace(self):
        self.assertEqual("我们换个话题", strip_punctuation(" 我们，换个话题！ "))
        self.assertEqual("换个话题吧", strip_punctuation("换个话题吧。"))

    def test_empty_safe(self):
        self.assertEqual("", strip_punctuation(""))
        self.assertEqual("", strip_punctuation(None) if False else strip_punctuation(""))


class EditDistanceTest(unittest.TestCase):
    def test_basics(self):
        self.assertEqual(0, edit_distance("换个话题", "换个话题"))
        self.assertEqual(2, edit_distance("换个话题", "换个画图"))
        self.assertEqual(3, edit_distance("abc", "xyz"))
        self.assertEqual(2, edit_distance("", "ab"))


class MatchNewTopicTest(unittest.TestCase):
    def test_exact_hit(self):
        self.assertTrue(match_new_topic("我们换个话题", exact=EXACT, fuzzy=FUZZY))

    def test_exact_hit_with_punctuation_noise(self):
        self.assertTrue(match_new_topic("我们，换个话题！", exact=EXACT, fuzzy=FUZZY))

    def test_fuzzy_hit_within_distance(self):
        self.assertTrue(match_new_topic("换个话题吧", exact=EXACT, fuzzy=FUZZY))
        self.assertTrue(match_new_topic("聊点别的吧", exact=EXACT, fuzzy=FUZZY))

    def test_asr_mistranscription_within_distance(self):
        # 「换个画图」dist=2（话↔画、题↔图）——ASR 误转写自动命中（spec 决策）
        self.assertTrue(match_new_topic("换个画图", exact=EXACT, fuzzy=FUZZY))

    def test_long_sentence_never_fuzzy(self):
        # 防误拦核心门槛：>8 字永不进模糊通道，即使包含暗号子串
        self.assertFalse(match_new_topic("我想给你讲个换话题的笑话", exact=EXACT, fuzzy=FUZZY))
        self.assertFalse(match_new_topic("换个话题怎么用英语说", exact=EXACT, fuzzy=FUZZY))

    def test_long_exact_sentence_only_matches_exact(self):
        self.assertFalse(match_new_topic("我们换个话题吧好不好", exact=EXACT, fuzzy=FUZZY))

    def test_unrelated_text_misses(self):
        self.assertFalse(match_new_topic("今天天气怎么样", exact=EXACT, fuzzy=FUZZY))
        self.assertFalse(match_new_topic("下一首歌曲", exact=EXACT, fuzzy=FUZZY))

    def test_empty_text_and_empty_tables(self):
        self.assertFalse(match_new_topic("", exact=EXACT, fuzzy=FUZZY))
        self.assertFalse(match_new_topic("，。！", exact=EXACT, fuzzy=FUZZY))
        self.assertFalse(match_new_topic("换个话题", exact=[], fuzzy=[]))
        self.assertFalse(match_new_topic("换个话题", exact=None, fuzzy=None))


if __name__ == "__main__":
    unittest.main()
