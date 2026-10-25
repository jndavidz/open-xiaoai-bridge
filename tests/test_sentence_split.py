"""句读切分器（P1 ticket 03）契约测试——纯函数、纯内存。

规则用例集覆盖（spec：强标点 ≥4 字即切 / 弱标点 ≥12 字切 / ≥20 字强制切）：
边界字数、标点组合、无标点长句强切、分片跨边界、空 delta、flush 尾段。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils.sentence_split import SentenceSegmenter  # noqa: E402


class StrongPunctTest(unittest.TestCase):
    """强标点（。！？；换行）：段内 ≥4 字（含标点）即切。"""

    def test_strong_punct_at_threshold_cuts(self):
        seg = SentenceSegmenter()
        self.assertEqual(["好的好的。"], seg.feed("好的好的。"))
        self.assertEqual("", seg.pending)

    def test_each_strong_punct_cuts(self):
        for punct in "。！？；\n":
            seg = SentenceSegmenter()
            self.assertEqual([f"今天天气不错{punct}"], seg.feed(f"今天天气不错{punct}"), punct)

    def test_short_strong_punct_accumulates(self):
        # 「好。」只有 2 字：不切，等后续凑足或 flush
        seg = SentenceSegmenter()
        self.assertEqual([], seg.feed("好。"))
        self.assertEqual("好。", seg.pending)

    def test_short_then_long_joins_into_one_segment(self):
        # 「好。」不切 → 后续强标点时整段（含前面的短句）一起切
        seg = SentenceSegmenter()
        self.assertEqual(["好。今天天气不错。"], seg.feed("好。今天天气不错。"))

    def test_multiple_segments_in_one_feed(self):
        seg = SentenceSegmenter()
        self.assertEqual(
            ["第一句话了。", "第二句话了。"],
            seg.feed("第一句话了。第二句话了。"),
        )


class WeakPunctTest(unittest.TestCase):
    """弱标点（，、：,;）：段内 ≥12 字（含标点）才切。"""

    def test_weak_punct_at_threshold_cuts(self):
        seg = SentenceSegmenter()
        text = "一二三四五六七八九十一，"  # 11 字 + 逗号 = 12
        self.assertEqual([text], seg.feed(text))

    def test_weak_punct_below_threshold_keeps(self):
        seg = SentenceSegmenter()
        self.assertEqual([], seg.feed("一，二，"))  # 各处均 <12
        self.assertEqual("一，二，", seg.pending)

    def test_each_weak_punct_cuts_at_threshold(self):
        prefix = "一二三四五六七八九零"  # 10 字
        for punct in "，、：,;":
            seg = SentenceSegmenter()
            self.assertEqual([f"{prefix}一{punct}"], seg.feed(f"{prefix}一{punct}"), punct)

    def test_strong_beats_weak_when_earlier(self):
        # 强标点先出现且够门槛：在强标点处切（不等到后面的弱标点）
        seg = SentenceSegmenter()
        text = "好的好的。然后这里继续说，"
        self.assertEqual(["好的好的。"], seg.feed(text))
        self.assertEqual("然后这里继续说，", seg.pending)


class ForceCutTest(unittest.TestCase):
    """无标点长句 ≥20 字强制切。"""

    def test_force_cut_at_20_chars(self):
        seg = SentenceSegmenter()
        text = "一" * 25
        self.assertEqual(["一" * 20], seg.feed(text))
        self.assertEqual("一" * 5, seg.pending)

    def test_below_force_cut_waits(self):
        seg = SentenceSegmenter()
        self.assertEqual([], seg.feed("一" * 19))
        self.assertEqual("一" * 19, seg.pending)

    def test_punct_before_force_cut_wins(self):
        # 第 22 字才有强标点：标点处切（22 > 20？不——扫描先遇强标点 i=21，
        # i+1=22 ≥4 → 切 22 字），剩余继续按规则
        seg = SentenceSegmenter()
        text = "一" * 21 + "。"
        self.assertEqual([text], seg.feed(text))


class StreamFeedTest(unittest.TestCase):
    """流式 feed 语义：分片跨边界、空 delta、flush 尾段。"""

    def test_delta_split_across_boundary(self):
        seg = SentenceSegmenter()
        self.assertEqual([], seg.feed("今天"))
        self.assertEqual([], seg.feed("天气"))
        self.assertEqual([], seg.feed("很好"))
        self.assertEqual(["今天天气很好。"], seg.feed("。"))

    def test_empty_delta_is_noop(self):
        seg = SentenceSegmenter()
        self.assertEqual([], seg.feed(""))
        self.assertEqual([], seg.feed(None or ""))
        self.assertEqual("", seg.pending)

    def test_flush_returns_tail_and_clears(self):
        seg = SentenceSegmenter()
        seg.feed("好的好的。")
        self.assertEqual([], seg.feed("还没说完的半句"))
        self.assertEqual("还没说完的半句", seg.pending)
        self.assertEqual("还没说完的半句", seg.flush())
        self.assertIsNone(seg.flush())  # 幂等：已清空
        self.assertEqual("", seg.pending)

    def test_segment_then_tail(self):
        seg = SentenceSegmenter()
        segments = seg.feed("第一句话了。第二句话还没")
        self.assertEqual(["第一句话了。"], segments)
        self.assertEqual("第二句话还没", seg.flush())

    def test_same_input_same_output(self):
        deltas = ["今天天气", "很好，我们", "出去玩", "吧。明天见。"]
        a, b = SentenceSegmenter(), SentenceSegmenter()
        out_a = [s for d in deltas for s in a.feed(d)] + [a.flush()]
        out_b = [s for d in deltas for s in b.feed(d)] + [b.flush()]
        self.assertEqual(out_a, out_b)


if __name__ == "__main__":
    unittest.main()
