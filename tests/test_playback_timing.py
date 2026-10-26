"""playback_timing 纯函数原语测试（B-29 步 1 / 评审 S4）。

pcm_duration_seconds 表驱动：正常 / 空 / 奇数字节 / 自定义采样率 / 非法参数；
estimate_playback_budget_ms 与迁出前口径一致（中文 4.5 字/秒 + 6s，timeout 硬上限）。
"""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.playback_timing import (  # noqa: E402
    estimate_playback_budget_ms,
    pcm_duration_seconds,
    sleep_playback_done,
)


class PcmDurationTest(unittest.TestCase):
    def test_normal_int16_mono(self):
        # 24000 采样 int16 = 48000 字节 = 恰好 1 秒
        self.assertAlmostEqual(1.0, pcm_duration_seconds(b"\x00" * 48000))

    def test_empty_buffer_is_zero(self):
        self.assertEqual(0.0, pcm_duration_seconds(b""))

    def test_odd_byte_count_truncates_to_whole_frames(self):
        # 48001 字节 = 24000.5 采样 → 整帧向下取 = 1.0s（截尾字节不算采样）
        self.assertAlmostEqual(1.0, pcm_duration_seconds(b"\x00" * 48001))

    def test_custom_sample_rate_and_width(self):
        # 8000Hz、4 字节/帧（如 int32）：32000 字节 = 8000 帧 = 1s
        self.assertAlmostEqual(1.0, pcm_duration_seconds(b"\x00" * 32000, sample_rate=8000, sample_width=4))

    def test_invalid_params_are_zero_not_error(self):
        self.assertEqual(0.0, pcm_duration_seconds(b"\x00" * 100, sample_rate=0))
        self.assertEqual(0.0, pcm_duration_seconds(b"\x00" * 100, sample_width=0))

    def test_sleep_playback_done_zero_pcm_returns_fast(self):
        asyncio.run(asyncio.wait_for(sleep_playback_done(b""), timeout=0.05))

    def test_sleep_playback_done_waits_pcm_duration(self):
        pcm = b"\x00" * (24000 * 2 // 100)  # 10ms
        async def run():
            loop = asyncio.get_running_loop()
            start = loop.time()
            await sleep_playback_done(pcm)
            return loop.time() - start

        elapsed = asyncio.run(run())
        self.assertGreaterEqual(elapsed, 0.009)


class EstimateBudgetTest(unittest.TestCase):
    """口径守门：与 speaker.py 迁出前逐值一致（re-export 兼容的前提）。"""

    def test_short_text_floors_at_min(self):
        self.assertEqual(8000, estimate_playback_budget_ms("你好", None))

    def test_long_text_caps_at_max(self):
        # 200 字 ≈ 50.4s（未到上限）；600 字 ≈ 139s → 封顶 120s
        self.assertAlmostEqual(50_444.44, estimate_playback_budget_ms("字" * 200, None), places=2)
        self.assertEqual(120_000, estimate_playback_budget_ms("字" * 600, None))

    def test_timeout_is_hard_cap(self):
        self.assertEqual(300, estimate_playback_budget_ms("你好", 300))

    def test_timeout_none_returns_estimate(self):
        self.assertEqual(8000, estimate_playback_budget_ms("你好", 600_000))

    def test_speaker_reexport_matches(self):
        from core.services import speaker as speaker_module

        self.assertEqual(
            estimate_playback_budget_ms("你好", None),
            speaker_module.estimate_playback_budget_ms("你好", None),
        )


if __name__ == "__main__":
    unittest.main()
