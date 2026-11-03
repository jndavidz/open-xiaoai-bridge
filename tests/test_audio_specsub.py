"""谱减回声抑制的契约测试（stdlib unittest + numpy，零外部服务）。

覆盖（对应 music-far 票 04 的验收面）：
  1. 参考全零（非播放期）→ 逐样本直通，不计帧；
  2. 参考恒定（无效参考）→ 直通；
  3. 合成信号：mic = near_voice + 0.5·ref 时，谱减后参考分量被显著削弱，
     且 near_voice 保留（不整体归零）；
  4. 输出为 1ch int16、稳态下输入/输出等量；
  5. stats 字段齐全（ERLE/延迟/旁路计数）；
  6. 参数校验：声道越界、mic==ref 应报错。
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.specsub import SpectralSuppressor, N_FFT, HOP  # noqa: E402

RATE = 16000


def _interleave(mics: np.ndarray, ref: np.ndarray, ref_ch: int = 6, channels: int = 8) -> bytes:
    """把 (n,) 的近端与参考组装成 channels 路交错 int16 PCM。"""
    n = min(len(mics), len(ref))
    buf = np.zeros((n, channels), dtype=np.int16)
    mic_cols = [c for c in range(channels) if c != ref_ch]
    for c in mic_cols:
        buf[:, c] = np.clip(mics[:n], -32768, 32767).astype(np.int16)
    buf[:, ref_ch] = np.clip(ref[:n], -32768, 32767).astype(np.int16)
    return buf.tobytes()


def _tone(freq: float, seconds: float, amp: float, rng: np.random.Generator) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    base = amp * np.sin(2 * np.pi * freq * t)
    return base + rng.normal(0, amp * 0.05, size=base.shape)


class SpectralSuppressorTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(7)

    def test_zero_reference_passes_through(self):
        sup = SpectralSuppressor(channels=8, ref_channel=6, mic_channel=0)
        voice = _tone(300, 0.5, 4000, self.rng)
        pcm = _interleave(voice, np.zeros_like(voice))
        out = np.frombuffer(sup.feed(pcm), dtype=np.int16)
        self.assertEqual(out.size, voice.size)
        np.testing.assert_array_equal(out, voice.astype(np.int16))
        st = sup.stats()
        self.assertGreaterEqual(st["bypass_no_ref"], 1)
        self.assertEqual(st["frames"], 0)

    def test_flat_reference_passes_through(self):
        sup = SpectralSuppressor(channels=8, ref_channel=6, mic_channel=0)
        voice = _tone(300, 0.5, 4000, self.rng)
        pcm = _interleave(voice, np.full_like(voice, 1234.0))
        out = np.frombuffer(sup.feed(pcm), dtype=np.int16)
        np.testing.assert_array_equal(out, voice.astype(np.int16))
        self.assertGreaterEqual(sup.stats()["bypass_ref_flat"], 1)

    def test_reference_is_attenuated_but_voice_survives(self):
        """mic = voice + 0.5·ref：谱减后参考分量显著削弱，近端仍在。"""
        sup = SpectralSuppressor(channels=8, ref_channel=6, mic_channel=0, alpha=0.8)
        ref = _tone(1000, 2.0, 6000, self.rng)          # 音乐（1 kHz 纯音）
        voice = _tone(250, 2.0, 3000, self.rng)         # 近端（250 Hz，与音乐分离便于度量）
        mic = voice + 0.5 * ref
        pcm = _interleave(mic, 0.5 * ref)

        out_chunks = []
        step = 512 * 8 * 2  # 每帧 512 样本 × 8ch × 2B
        data = pcm
        for i in range(0, len(data), step):
            out_chunks.append(sup.feed(data[i:i + step]))
        out = np.frombuffer(b"".join(out_chunks), dtype=np.int16).astype(np.float64)
        self.assertGreater(out.size, RATE)  # 至少 1 秒输出

        def band_energy(x, f0):
            spec = np.abs(np.fft.rfft(x[: RATE]))
            freqs = np.fft.rfftfreq(RATE, 1 / RATE)
            m = (freqs > f0 * 0.9) & (freqs < f0 * 1.1)
            return float((spec[m] ** 2).sum())

        ref_before = band_energy(mic.astype(np.float64), 1000)
        ref_after = band_energy(out, 1000)
        voice_before = band_energy(mic.astype(np.float64), 250)
        voice_after = band_energy(out, 250)

        self.assertLess(ref_after / max(ref_before, 1e-9), 0.5,
                        "参考分量应被削弱一半以上")
        self.assertGreater(voice_after / max(voice_before, 1e-9), 0.3,
                           "近端语音不应被整体抹掉")

    def test_output_is_mono_int16(self):
        sup = SpectralSuppressor(channels=8, ref_channel=6, mic_channel=3)
        ref = _tone(800, 0.3, 5000, self.rng)
        mic = _tone(400, 0.3, 3000, self.rng)
        pcm = _interleave(mic, ref)
        out = sup.feed(pcm)
        self.assertEqual(len(out) % 2, 0)
        arr = np.frombuffer(out, dtype=np.int16)
        self.assertLessEqual(arr.size, mic.size)       # 首帧延迟，不凭空变长
        self.assertGreater(arr.size, 0)

    def test_stats_fields(self):
        sup = SpectralSuppressor(channels=8, ref_channel=7, mic_channel=0)
        st = sup.stats()
        for key in ("frames", "bypass_no_ref", "bypass_ref_flat",
                    "erle_db", "latency_ms", "alpha", "ref_channel"):
            self.assertIn(key, st)
        self.assertEqual(st["ref_channel"], 7)
        self.assertAlmostEqual(st["latency_ms"], round(N_FFT * 1000.0 / RATE, 1))

    def test_reset_clears_state(self):
        sup = SpectralSuppressor(channels=8, ref_channel=6, mic_channel=0)
        ref = _tone(700, 0.5, 5000, self.rng)
        mic = _tone(300, 0.5, 3000, self.rng)
        sup.feed(_interleave(mic, ref))
        self.assertGreater(sup.stats()["frames"], 0)
        sup.reset()
        self.assertEqual(sup.stats()["frames"], 0)

    def test_invalid_params(self):
        with self.assertRaises(ValueError):
            SpectralSuppressor(channels=8, ref_channel=9)
        with self.assertRaises(ValueError):
            SpectralSuppressor(channels=8, ref_channel=6, mic_channel=6)
        with self.assertRaises(ValueError):
            SpectralSuppressor(channels=1, ref_channel=0)


if __name__ == "__main__":
    unittest.main()
