"""六麦波束成形（beamform_fixed / beamform_doa）的行为测试。

只测 public seam：`ChannelDownmixer.feed()` / `flush()` / `doa_delays()` 的
6ch→1ch 行为。合成 6 路带已知方向/时延的信号 + 独立噪声，断言波束输出相对
最强单麦有可测的信噪比提升——这是「波束是否真的对齐了」的外部可观察证据。
阵列坐标在测试内独立给出（与固件文档一致），不依赖被测模块的常量。
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.channels import ChannelDownmixer  # noqa: E402

CHANNELS = 6
RATE = 16000
SOUND_SPEED_MPS = 343.0
# 与 docs/devices/lx06/mic-array-6ch-capture.md §6.5 一致：正六边形、r=35mm、
# ch0=(0,+0.035) 起顺时针。
COORDS_M = np.array(
    [
        [0.0, 0.035],
        [0.035 * np.cos(np.pi / 6), 0.035 * np.sin(np.pi / 6)],
        [0.035 * np.cos(np.pi / 6), -0.035 * np.sin(np.pi / 6)],
        [0.0, -0.035],
        [-0.035 * np.cos(np.pi / 6), -0.035 * np.sin(np.pi / 6)],
        [-0.035 * np.cos(np.pi / 6), 0.035 * np.sin(np.pi / 6)],
    ]
)


def _expected_delays(angle_deg: float) -> np.ndarray:
    theta = np.deg2rad(angle_deg)
    direction = np.array([np.sin(theta), np.cos(theta)])
    return COORDS_M @ direction / SOUND_SPEED_MPS * RATE


def _source(n: int) -> np.ndarray:
    """带限类语音信号：多个谐波正弦。"""
    t = np.arange(n) / RATE
    s = np.zeros(n, dtype=np.float64)
    for freq in (300.0, 700.0, 1200.0, 1800.0):
        s += np.sin(2 * np.pi * freq * t + freq)
    return s / np.max(np.abs(s))


def _channel_with_delay(s: np.ndarray, delay_samples: float) -> np.ndarray:
    """按分数样本延迟重采样（独立于被测实现的插值方式）。"""
    index = np.arange(len(s), dtype=np.float64) + delay_samples
    return np.interp(index, np.arange(len(s), dtype=np.float64), s)


def _mic_matrix(coords, angle_deg: float, snr_db: float, seed: int):
    """(n, 6) float 麦克风矩阵：同一平面波按 `coords` 逐通道到达 + 独立噪声。"""
    rng = np.random.default_rng(seed)
    n = RATE * 2
    s = _source(n)
    theta = np.deg2rad(angle_deg)
    direction = np.array([np.sin(theta), np.cos(theta)])  # 0° = 正前方
    noise_sigma = np.sqrt(np.mean(s**2)) * (10 ** (-snr_db / 20.0))
    rows = []
    for coord in coords:
        delay = float(np.dot(np.asarray(coord, dtype=np.float64), direction) / SOUND_SPEED_MPS * RATE)
        channel = _channel_with_delay(s, delay)
        channel = channel + rng.normal(0.0, noise_sigma, n)
        rows.append(np.clip(channel, -1.0, 1.0))
    return np.stack(rows, axis=1), s


def _six_channel_pcm(
    angle_deg: float, snr_db: float = 0.0, seed: int = 7, coords_m=None
):
    """生成六麦交错 int16 PCM：同一平面波按阵列几何到达各麦。

    `coords_m` 为逐通道坐标表（标定映射）；缺省用测试顶点表原序。
    """
    coords = COORDS_M if coords_m is None else coords_m
    rows, source = _mic_matrix(coords, angle_deg, snr_db, seed)
    return (rows * 32767).astype(np.int16).tobytes(), source


def _aligned_snr(output_pcm: bytes, reference: np.ndarray) -> float:
    """波束会引入固定组延迟：先按互相关对齐，再算 SNR。"""
    y = np.frombuffer(output_pcm, dtype=np.int16).astype(np.float64) / 32767.0
    n = min(len(y), len(reference))
    y, ref = y[:n], reference[:n]
    lags = np.arange(-32, 33)
    corr = [abs(np.dot(y[32 + lag : n - 32 + lag], ref[32 : n - 32])) for lag in lags]
    best = lags[int(np.argmax(corr))]
    y = np.roll(y, -best)
    mse = float(np.mean((y - ref) ** 2))
    if mse <= 0:
        return 99.0
    return 10 * np.log10(float(np.mean(ref**2)) / mse)


def _feed_chunked(mixer: ChannelDownmixer, pcm: bytes, chunk_bytes: int = 8640) -> bytes:
    """按 bridge 实际块大小（1440 样本 × 6ch × 2B）流式喂入。"""
    out = bytearray()
    for offset in range(0, len(pcm), chunk_bytes):
        out += mixer.feed(pcm[offset : offset + chunk_bytes])
    out += mixer.flush()
    return bytes(out)


CALIBRATED_PERM = (2, 1, 0, 5, 4, 3)  # lx06-mic-array-frontend 票 01 标定结论
CALIBRATED_COORDS_M = [tuple(COORDS_M[index]) for index in CALIBRATED_PERM]


def _eight_channel_pcm(
    angle_deg: float = 0.0,
    snr_db: float = 0.0,
    seed: int = 7,
    coords_m=None,
    ref_level: float = 0.9,
):
    """生产口径 8ch 交错 int16 PCM：前六路 = 麦阵列平面波，ch6/ch7 = 播放参考。

    参考路是与近端**完全无关**的大振幅正弦（模拟正在播放的音乐）；同一
    `seed`/`coords_m` 下麦克风侧逐样本可复现，只有参考路随 `ref_level` 变。
    """
    coords = COORDS_M if coords_m is None else coords_m
    mics, source = _mic_matrix(coords, angle_deg, snr_db, seed)
    count = len(mics)
    t = np.arange(count) / RATE
    rng = np.random.default_rng(seed + 1)
    refs = []
    for index in range(2):
        tone = np.sin(2 * np.pi * 220.0 * (index + 1) * t + rng.uniform(0.0, 6.28))
        refs.append(np.clip(ref_level * tone, -1.0, 1.0))
    rows = np.concatenate([mics, np.stack(refs, axis=1)], axis=1)
    return (rows * 32767).astype(np.int16).tobytes(), source


class BeamformEightChannelTest(unittest.TestCase):
    """票 01（lx06-beamform-8ch）：波束直接吃生产那条 8 路流。

    只断言外部可观察性质：能不能构建、输出长度/内容、以及「换掉播放参考路
    输出必须逐字节不变」。内部通道索引不在断言范围内。
    """

    def _beam_mixer(self, **kwargs):
        params = {
            "channels": 8,
            "strategy": "beamform_fixed",
            "reference_channels": (6, 7),
            "beam_coords_m": CALIBRATED_COORDS_M,
        }
        params.update(kwargs)
        return ChannelDownmixer(**params)

    def test_builds_and_runs_on_eight_channel_capture(self):
        pcm, _ = _eight_channel_pcm(coords_m=CALIBRATED_COORDS_M)
        mixer = self._beam_mixer()
        out = _feed_chunked(mixer, pcm, chunk_bytes=1440 * 8 * 2)
        self.assertEqual(len(pcm) // (8 * 2) * 2, len(out), "8ch 帧流应产出等长样本的 1ch")
        self.assertIsNone(mixer.current_channel, "波束下 current_channel 无意义，不得填误导值")

    def test_reference_channels_never_reach_the_beam(self):
        """同一批麦克风信号、不同的播放参考能量 ⇒ 波束输出逐字节相同。"""
        quiet, _ = _eight_channel_pcm(coords_m=CALIBRATED_COORDS_M, ref_level=0.01)
        loud, _ = _eight_channel_pcm(coords_m=CALIBRATED_COORDS_M, ref_level=0.98)
        quiet_rows = np.frombuffer(quiet, dtype=np.int16).reshape(-1, 8)
        loud_rows = np.frombuffer(loud, dtype=np.int16).reshape(-1, 8)
        np.testing.assert_array_equal(quiet_rows[:, :6], loud_rows[:, :6])
        self.assertFalse(np.array_equal(quiet_rows[:, 6], loud_rows[:, 6]))

        out_quiet = _feed_chunked(self._beam_mixer(), quiet, chunk_bytes=23040)
        out_loud = _feed_chunked(self._beam_mixer(), loud, chunk_bytes=23040)
        self.assertEqual(out_quiet, out_loud, "参考声道进入了波束计算")

    def test_eight_channel_without_reference_declaration_is_rejected(self):
        """忘声明参考声道 = 六路坐标配八路参与通道 ⇒ 显式报错，不静默错位波束。"""
        with self.assertRaises(ValueError):
            ChannelDownmixer(
                channels=8,
                strategy="beamform_fixed",
                beam_coords_m=CALIBRATED_COORDS_M,
            )

    def test_padding_mic_stream_to_eight_channels_is_output_identical(self):
        """6ch 路径行为不变：同样的六路补零成 8ch，波束输出逐字节相同。"""
        six, _ = _six_channel_pcm(0.0, snr_db=10.0, seed=19, coords_m=CALIBRATED_COORDS_M)
        rows = np.frombuffer(six, dtype=np.int16).reshape(-1, CHANNELS)
        padded = np.concatenate([rows, np.zeros((len(rows), 2), dtype=np.int16)], axis=1)
        eight = padded.astype(np.int16).tobytes()

        out_six = _feed_chunked(
            ChannelDownmixer(
                channels=CHANNELS,
                strategy="beamform_fixed",
                beam_coords_m=CALIBRATED_COORDS_M,
            ),
            six,
        )
        out_eight = _feed_chunked(self._beam_mixer(), eight, chunk_bytes=1440 * 8 * 2)
        self.assertEqual(out_six, out_eight, "8ch 兼容改动改变了 6ch 波束输出")

    def test_doa_strategy_also_builds_on_eight_channel_capture(self):
        pcm, _ = _eight_channel_pcm(coords_m=CALIBRATED_COORDS_M, seed=9)
        mixer = self._beam_mixer(strategy="beamform_doa")
        out = _feed_chunked(mixer, pcm, chunk_bytes=1440 * 8 * 2)
        self.assertEqual(len(pcm) // (8 * 2) * 2, len(out))

    def test_explicit_subset_excludes_a_non_reference_mic(self):
        """参与通道可显式收窄：不在子集里的麦（哪怕不是参考路）也不影响输出。

        坐标第 i 行对应 `beam_channels` 第 i 个声道——这是调用方的契约，
        收窄时必须一并给出按同序投影过的坐标表。
        """
        five = CALIBRATED_COORDS_M[:5]
        quiet, _ = _eight_channel_pcm(coords_m=CALIBRATED_COORDS_M, seed=21)
        rows_quiet = np.frombuffer(quiet, dtype=np.int16).reshape(-1, 8)
        rows_loud = rows_quiet.copy()
        rows_loud[:, 5] = np.clip(rows_loud[:, 5] * 4, -32767, 32767).astype(np.int16)
        self.assertFalse(np.array_equal(rows_quiet[:, 5], rows_loud[:, 5]))

        kwargs = dict(
            channels=8,
            strategy="beamform_fixed",
            reference_channels=(6, 7),
            beam_channels=(0, 1, 2, 3, 4),
            beam_coords_m=five,
        )
        out_quiet = _feed_chunked(ChannelDownmixer(**kwargs), rows_quiet.tobytes(), 23040)
        out_loud = _feed_chunked(ChannelDownmixer(**kwargs), rows_loud.tobytes(), 23040)
        self.assertEqual(out_quiet, out_loud, "ch5 已退出参与通道，却仍进入波束")

    def test_beam_needs_at_least_two_participating_channels(self):
        """反例：参与通道只剩一路时不做「波束」（一路无从谈相干增益）。"""
        with self.assertRaises(ValueError):
            ChannelDownmixer(
                channels=8,
                strategy="beamform_fixed",
                reference_channels=(1, 2, 3, 4, 5, 6, 7),
            )

    def _gain(self, coords_beam, seed=17, snr_db=0.0) -> float:
        pcm, source = _eight_channel_pcm(
            0.0, snr_db=snr_db, seed=seed, coords_m=CALIBRATED_COORDS_M
        )
        base = ChannelDownmixer(
            channels=8, strategy="strongest", reference_channels=(6, 7)
        )
        beam = self._beam_mixer(beam_coords_m=coords_beam)
        snr_base = _aligned_snr(_feed_chunked(base, pcm, 23040), source)
        snr_beam = _aligned_snr(_feed_chunked(beam, pcm, 23040), source)
        return snr_beam - snr_base

    def test_eight_channel_coherent_gain_matches_theory(self):
        gain = self._gain(CALIBRATED_COORDS_M)
        self.assertGreater(
            gain,
            6.0,
            f"8ch 采集下标定几何的相干增益 {gain:.2f} dB 未接近理论值 7.8 dB",
        )

    def test_eight_channel_wrong_mapping_degrades_gain(self):
        gain_correct = self._gain(CALIBRATED_COORDS_M)
        gain_wrong = self._gain([tuple(COORDS_M[index]) for index in range(6)])
        self.assertLess(
            gain_wrong,
            gain_correct - 3.0,
            f"错映射增益 {gain_wrong:.2f} dB 未显著退化（正确 {gain_correct:.2f} dB）"
            "——这条测试没有区分力",
        )


class BeamformFixedTest(unittest.TestCase):
    def test_beamform_fixed_improves_snr_on_known_direction(self):
        pcm, source = _six_channel_pcm(angle_deg=0.0)
        baseline = ChannelDownmixer(channels=CHANNELS, strategy="strongest")
        beamformed = ChannelDownmixer(channels=CHANNELS, strategy="beamform_fixed")

        snr_base = _aligned_snr(_feed_chunked(baseline, pcm), source)
        snr_beam = _aligned_snr(_feed_chunked(beamformed, pcm), source)

        self.assertGreater(
            snr_beam - snr_base,
            3.0,
            f"beamform_fixed 未达到保守增益：base={snr_base:.1f}dB beam={snr_beam:.1f}dB",
        )


class BeamformDoaTest(unittest.TestCase):
    def test_beamform_doa_improves_snr_on_unknown_direction(self):
        # 声源在 40°（非固定指向），DOA 必须自己估出方向才能获得增益。
        pcm, source = _six_channel_pcm(angle_deg=40.0, seed=11)
        baseline = ChannelDownmixer(channels=CHANNELS, strategy="strongest")
        beamformed = ChannelDownmixer(channels=CHANNELS, strategy="beamform_doa")

        snr_base = _aligned_snr(_feed_chunked(baseline, pcm), source)
        snr_beam = _aligned_snr(_feed_chunked(beamformed, pcm), source)

        self.assertGreater(
            snr_beam - snr_base,
            3.0,
            f"beamform_doa 未达到保守增益：base={snr_base:.1f}dB beam={snr_beam:.1f}dB",
        )

    def test_beamform_doa_falls_back_to_fixed_after_silence(self):
        pcm, _ = _six_channel_pcm(angle_deg=40.0, seed=11)
        mixer = ChannelDownmixer(channels=CHANNELS, strategy="beamform_doa")
        _feed_chunked(mixer, pcm)
        silence = np.zeros((RATE * 2, CHANNELS), dtype=np.int16).tobytes()
        _feed_chunked(mixer, silence)

        delays = np.asarray(mixer.doa_delays())
        expected = _expected_delays(0.0)
        residual = np.max(
            np.abs((delays - delays.mean()) - (expected - expected.mean()))
        )
        self.assertLess(residual, 0.5, f"DOA 未回落到固定指向：残差 {residual:.2f} 样本")

    def test_beamform_doa_survives_silence_and_recovers(self):
        pcm, source = _six_channel_pcm(angle_deg=-35.0, seed=23)
        silence = np.zeros((RATE // 2, CHANNELS), dtype=np.int16).tobytes()
        beamformed = ChannelDownmixer(channels=CHANNELS, strategy="beamform_doa")
        baseline = ChannelDownmixer(channels=CHANNELS, strategy="strongest")

        out_silence = _feed_chunked(beamformed, silence)
        samples = np.frombuffer(out_silence, dtype=np.int16)
        self.assertTrue(np.all(np.isfinite(samples)))
        self.assertEqual(len(samples), RATE // 2)

        snr_base = _aligned_snr(_feed_chunked(baseline, pcm), source)
        snr_beam = _aligned_snr(_feed_chunked(beamformed, pcm), source)
        self.assertGreater(
            snr_beam - snr_base,
            3.0,
            f"静音后未恢复：base={snr_base:.1f}dB beam={snr_beam:.1f}dB",
        )


class BeamformContractTest(unittest.TestCase):
    def test_normalize_strategy_accepts_beamform(self):
        from core.services.audio.channels import normalize_strategy

        self.assertEqual("beamform_fixed", normalize_strategy("beamform_fixed"))
        self.assertEqual("beamform_doa", normalize_strategy("beamform_doa"))

    def test_single_channel_passthrough_ignores_beamform(self):
        pcm = np.arange(1000, dtype=np.int16).tobytes()
        mixer = ChannelDownmixer(channels=1, strategy="beamform_fixed")
        self.assertEqual(pcm, mixer.feed(pcm))

    def test_beamform_needs_calibration_when_subset_is_not_six_mics(self):
        """非「6 路且无标定坐标」的组合一律拒绝：不允许猜一套几何。"""
        with self.assertRaises(ValueError):
            # 8ch 未声明参考声道 ⇒ 参与通道 8 路，兼容默认几何只有 6 个点
            ChannelDownmixer(channels=8, strategy="beamform_fixed")

    def test_beamform_rejects_reference_channel_inside_subset(self):
        """反例：参考声道混进参与通道 ⇒ 显式报错，不得静默产出错误波形。"""
        with self.assertRaises(ValueError):
            ChannelDownmixer(
                channels=8,
                strategy="beamform_fixed",
                reference_channels=(6, 7),
                beam_channels=(0, 1, 2, 3, 4, 6),
                beam_coords_m=CALIBRATED_COORDS_M,
            )

    def test_flush_drops_unaligned_multichannel_residue(self):
        mixer = ChannelDownmixer(channels=CHANNELS, strategy="beamform_fixed")
        self.assertEqual(b"", mixer.feed(b"\x01" * 11))
        self.assertEqual(b"", mixer.flush())

    def test_flush_keeps_single_channel_odd_tail(self):
        mixer = ChannelDownmixer(channels=1, strategy="strongest")
        self.assertEqual(b"\x01\x02", mixer.feed(b"\x01\x02\x03"))
        self.assertEqual(b"\x03", mixer.flush())


class CalibratedMappingTest(unittest.TestCase):
    """票 02：波束按标定映射（channel_map_lx06.json）对齐的合成验收。

    信号按标定映射合成（通道 c 在顶点 PERM[c]），波束喂同一映射 ⇒
    目标方向相干增益应接近理论值 10·log10(6) ≈ 7.8 dB；喂旧假设映射
    （恒等）⇒ 显著退化——把「映射对不对」从真机实验降级为单元测试。
    """

    def _coords(self, perm):
        return [tuple(COORDS_M[i]) for i in perm]

    def _gain(self, coords_signal, coords_beam, snr_db=0.0, seed=13):
        pcm, source = _six_channel_pcm(
            0.0, snr_db=snr_db, seed=seed, coords_m=coords_signal
        )
        baseline = ChannelDownmixer(channels=CHANNELS, strategy="strongest")
        beam = ChannelDownmixer(
            channels=CHANNELS,
            strategy="beamform_fixed",
            beam_coords_m=coords_beam,
        )
        snr_base = _aligned_snr(_feed_chunked(baseline, pcm), source)
        snr_beam = _aligned_snr(_feed_chunked(beam, pcm), source)
        return snr_beam - snr_base

    def test_calibrated_mapping_reaches_theoretical_coherent_gain(self):
        # snr=0 工作点（噪声主导）下相干增益实测 ≈7.7 dB；高 SNR 下插值
        # 失真主导会让测得增益缩水（失真是信号相关的，不是空间白噪声）。
        gain = self._gain(self._coords(CALIBRATED_PERM), self._coords(CALIBRATED_PERM))
        self.assertGreater(
            gain,
            6.0,
            f"标定映射相干增益 {gain:.2f} dB 未接近理论值 7.8 dB",
        )

    def test_wrong_mapping_significantly_degrades_gain(self):
        gain_correct = self._gain(self._coords(CALIBRATED_PERM), self._coords(CALIBRATED_PERM))
        # 旧代码假设 ch0=正上（恒等映射）：用错表去对齐按标定映射合成的信号
        gain_wrong = self._gain(self._coords(CALIBRATED_PERM), self._coords(range(6)))
        self.assertLess(
            gain_wrong,
            gain_correct - 3.0,
            f"错映射增益 {gain_wrong:.2f} dB 未显著退化（正确 {gain_correct:.2f} dB）"
            "——这条测试没有区分力",
        )

    def test_default_coords_preserved_when_not_provided(self):
        """不传映射 = 兼容默认几何（旧行为逐字节不变），但必须**出声**告警。"""
        pcm, _ = _six_channel_pcm(0.0, snr_db=20.0, seed=5)
        with self.assertWarns(RuntimeWarning) as caught:
            legacy = ChannelDownmixer(channels=CHANNELS, strategy="beamform_fixed")
        self.assertIn("兼容默认几何", str(caught.warning))
        explicit = ChannelDownmixer(
            channels=CHANNELS,
            strategy="beamform_fixed",
            beam_coords_m=self._coords(range(6)),
        )
        self.assertEqual(_feed_chunked(legacy, pcm), _feed_chunked(explicit, pcm))

    def test_non_beam_strategies_ignore_coords(self):
        """strongest/fixed/average 对 beam_coords_m 无感（回归边界）。"""
        pcm, _ = _six_channel_pcm(0.0, snr_db=20.0, seed=5)
        for strategy in ("strongest", "average"):
            plain = ChannelDownmixer(channels=CHANNELS, strategy=strategy)
            with_coords = ChannelDownmixer(
                channels=CHANNELS, strategy=strategy, beam_coords_m=self._coords(CALIBRATED_PERM)
            )
            self.assertEqual(
                _feed_chunked(plain, pcm),
                _feed_chunked(with_coords, pcm),
                f"{strategy} 行为被 beam_coords_m 改变",
            )

    def test_beam_coords_length_must_match_participating_channels(self):
        with self.assertRaises(ValueError):
            ChannelDownmixer(
                channels=CHANNELS,
                strategy="beamform_fixed",
                beam_coords_m=[(0.0, 0.035)] * 5,
            )


if __name__ == "__main__":
    unittest.main()
