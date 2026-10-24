"""六麦音频入口纯逻辑测试（ticket 04）。

Stdlib only：不连真机、不联网、不依赖 numpy。覆盖：
- 帧对齐 / 跨帧残留（不丢样本、不重复样本）
- 选麦迟滞
- strongest / average / fixed 三种降声道策略
- 1ch 回退直通
"""

import random
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.channels import (  # noqa: E402
    STRATEGY_AVERAGE,
    STRATEGY_FIXED,
    STRATEGY_STRONGEST,
    ChannelDownmixer,
    ChannelSelector,
    FrameAligner,
    frame_rms,
    normalize_strategy,
)


def pcm(rows) -> bytes:
    """rows: 每个元素是同一时刻各声道的 int16 样本序列。"""
    flat = [sample for row in rows for sample in row]
    return struct.pack("<" + "h" * len(flat), *flat)


def samples(pcm_bytes: bytes):
    count = len(pcm_bytes) // 2
    return list(struct.unpack("<" + "h" * count, pcm_bytes))


class FrameAlignerTest(unittest.TestCase):
    def test_aligned_input_passes_through(self):
        aligner = FrameAligner(channels=6)
        data = pcm([(1, 2, 3, 4, 5, 6), (7, 8, 9, 10, 11, 12)])
        self.assertEqual(data, aligner.feed(data))
        self.assertEqual(0, aligner.residual_bytes)

    def test_partial_frame_is_buffered(self):
        aligner = FrameAligner(channels=6)
        head = bytes(range(5))
        self.assertEqual(b"", aligner.feed(head))
        self.assertEqual(5, aligner.residual_bytes)

        tail = bytes(range(5, 12))
        self.assertEqual(head + tail, aligner.feed(tail))
        self.assertEqual(0, aligner.residual_bytes)

    def test_flush_returns_and_clears_residual(self):
        aligner = FrameAligner(channels=6)
        aligner.feed(b"\x01" * 7)
        self.assertEqual(b"\x01" * 7, aligner.flush())
        self.assertEqual(0, aligner.residual_bytes)
        self.assertEqual(b"", aligner.flush())

    def test_no_sample_lost_or_duplicated_across_random_chunks(self):
        channels = 6
        rows = [
            tuple((index * 7 + channel * 11) % 30000 for channel in range(channels))
            for index in range(200)
        ]
        original = pcm(rows)
        aligner = FrameAligner(channels=channels)

        rng = random.Random(20261024)
        offset = 0
        collected = bytearray()
        while offset < len(original):
            size = rng.randint(1, 17)
            collected.extend(aligner.feed(original[offset : offset + size]))
            offset += size
        collected.extend(aligner.flush())

        self.assertEqual(len(original), len(collected))
        self.assertEqual(original, bytes(collected))
        # 逐样本校验，确保不是「长度对但内容错位」
        self.assertEqual(samples(original), samples(bytes(collected)))

    def test_single_channel_keeps_odd_tail_byte(self):
        aligner = FrameAligner(channels=1)
        original = b"\x01\x02\x03\x04\x05"
        collected = bytearray()
        collected.extend(aligner.feed(original[:3]))
        collected.extend(aligner.feed(original[3:]))
        collected.extend(aligner.flush())
        self.assertEqual(original, bytes(collected))


class ChannelSelectorTest(unittest.TestCase):
    def test_first_frame_picks_loudest_channel(self):
        selector = ChannelSelector(channels=6)
        chosen = selector.update(pcm([(10, 20, 30, 900, 40, 50)]))
        self.assertEqual(3, chosen)

    def test_no_switch_when_candidate_is_not_clearly_better(self):
        selector = ChannelSelector(
            channels=2, window_frames=3, hysteresis_ratio=2.0, min_hold_frames=3
        )
        selector.update(pcm([(1000, 100)]))
        for _ in range(5):
            chosen = selector.update(pcm([(1000, 1500)]))
        self.assertEqual(0, chosen, "候选未超过迟滞阈值时不应切麦")

    def test_switches_after_sustained_stronger_candidate(self):
        selector = ChannelSelector(
            channels=2, window_frames=3, hysteresis_ratio=2.0, min_hold_frames=3
        )
        selector.update(pcm([(1000, 100)]))
        for _ in range(2):
            self.assertEqual(0, selector.update(pcm([(1000, 3000)])))
        # 第三帧连续领先且明显更强 → 切换
        self.assertEqual(1, selector.update(pcm([(1000, 3000)])))

    def test_single_frame_spike_does_not_switch(self):
        selector = ChannelSelector(
            channels=2, window_frames=3, hysteresis_ratio=1.2, min_hold_frames=3
        )
        selector.update(pcm([(1000, 100)]))
        self.assertEqual(0, selector.update(pcm([(1000, 9000)])))
        self.assertEqual(0, selector.update(pcm([(1000, 100)])))
        self.assertEqual(0, selector.update(pcm([(1000, 100)])))

    def test_rms_per_channel(self):
        levels = frame_rms(pcm([(300, 400)]), channels=2)
        self.assertAlmostEqual(300.0, levels[0], places=3)
        self.assertAlmostEqual(400.0, levels[1], places=3)


class ChannelDownmixerTest(unittest.TestCase):
    def test_average_strategy(self):
        mixer = ChannelDownmixer(channels=6, strategy=STRATEGY_AVERAGE)
        out = mixer.feed(pcm([(10, 20, 30, 40, 50, 60), (-10, -20, -30, -40, -50, -60)]))
        self.assertEqual([35, -35], samples(out))

    def test_average_rounds_symmetrically(self):
        mixer = ChannelDownmixer(channels=6, strategy=STRATEGY_AVERAGE)
        out = mixer.feed(pcm([(1, 2, 2, 2, 2, 2), (-1, -2, -2, -2, -2, -2)]))
        self.assertEqual([2, -2], samples(out))

    def test_fixed_strategy(self):
        mixer = ChannelDownmixer(
            channels=6, strategy=STRATEGY_FIXED, fixed_channel=2
        )
        out = mixer.feed(pcm([(10, 20, 30, 40, 50, 60), (1, 2, 3, 4, 5, 6)]))
        self.assertEqual([30, 3], samples(out))

    def test_fixed_channel_clamped_to_valid_range(self):
        mixer = ChannelDownmixer(
            channels=6, strategy=STRATEGY_FIXED, fixed_channel=99
        )
        self.assertEqual(5, mixer.fixed_channel)
        out = mixer.feed(pcm([(10, 20, 30, 40, 50, 60)]))
        self.assertEqual([60], samples(out))

    def test_strongest_strategy_tracks_loudest(self):
        mixer = ChannelDownmixer(channels=6, strategy=STRATEGY_STRONGEST)
        out = mixer.feed(pcm([(10, 20, 30, 40, 900, 50)]))
        self.assertEqual([900], samples(out))
        self.assertEqual(4, mixer.current_channel)

    def test_strongest_respects_hysteresis(self):
        mixer = ChannelDownmixer(
            channels=2,
            strategy=STRATEGY_STRONGEST,
            window_frames=3,
            hysteresis_ratio=2.0,
            min_hold_frames=3,
        )
        mixer.feed(pcm([(1000, 100)]))
        for _ in range(3):
            self.assertEqual([1000], samples(mixer.feed(pcm([(1000, 1500)]))))
        # 明显更强但未满足迟滞/连续帧数前不切
        self.assertEqual([1000], samples(mixer.feed(pcm([(1000, 3000)]))))

        switched = False
        for _ in range(6):
            if samples(mixer.feed(pcm([(1000, 3000)])))[0] == 3000:
                switched = True
                break
        self.assertTrue(switched, "持续明显更响的声道最终应被选中")
        self.assertEqual(1, mixer.current_channel)

    def test_misaligned_chunks_preserve_every_frame(self):
        mixer = ChannelDownmixer(channels=6, strategy=STRATEGY_FIXED, fixed_channel=1)
        rows = [(index, index + 1000, 0, 0, 0, 0) for index in range(50)]
        data = pcm(rows)

        rng = random.Random(7)
        offset = 0
        collected = bytearray()
        while offset < len(data):
            size = rng.randint(1, 13)
            collected.extend(mixer.feed(data[offset : offset + size]))
            offset += size
        collected.extend(mixer.aligner.flush())

        self.assertEqual([row[1] for row in rows], samples(bytes(collected)))

    def test_single_channel_passthrough(self):
        mixer = ChannelDownmixer(channels=1)
        data = pcm([(123,), (-456,)])
        self.assertEqual(data, mixer.feed(data))
        self.assertIsNone(mixer.current_channel)

    def test_reset_restores_initial_state(self):
        mixer = ChannelDownmixer(channels=2, strategy=STRATEGY_STRONGEST)
        mixer.feed(pcm([(10, 900)]))
        self.assertEqual(1, mixer.current_channel)
        mixer.reset()
        self.assertEqual(0, mixer.current_channel)
        self.assertEqual(0, mixer.frame_count)

    def test_normalize_strategy(self):
        self.assertEqual(STRATEGY_AVERAGE, normalize_strategy(" AVERAGE "))
        self.assertEqual(STRATEGY_FIXED, normalize_strategy("fixed"))
        self.assertEqual(STRATEGY_STRONGEST, normalize_strategy("bogus"))
        self.assertEqual(STRATEGY_STRONGEST, normalize_strategy(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
