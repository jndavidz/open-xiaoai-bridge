"""通道↔顶点映射标定器的行为测试（lx06-mic-array-frontend 票 01）。

被测对象 = deploy/lx06/kws-beamform-ab/calibrate_channel_map.py（numpy 离线工具，
按文件加载，与 test_eval_contract.py 同模式，避免 deploy 目录进 bridge import path）。

只测外部行为（spec Testing Decisions）：
- 合成信号进、排列出：已知方向 + 已知映射的多麦信号，标定器应还原该映射；
- 判据是数值边界（残差 <1 样本、最优组大小、720 穷举），不测 FFT 内部实现；
- 判据不满足时必须显式拒绝出结论（不得强行取最优解）。
"""

import importlib.util
import math
import unittest
from pathlib import Path

import numpy as np

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BRIDGE_ROOT.parent
CALIBRATOR = REPO_ROOT / "deploy/lx06/kws-beamform-ab/calibrate_channel_map.py"

_spec = importlib.util.spec_from_file_location("calibrate_channel_map", CALIBRATOR)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

RATE = 16000
N_SAMPLES = 16000  # 1 s / 段，合成自检用不着真机段长


def _perm_from_coords(coords_by_channel) -> tuple[int, ...]:
    """channel→顶点 下标排列（顶点表 = 标定器内六边形顶点顺序）。"""
    vertices = [tuple(round(v, 6) for v in point) for point in _mod.HEXAGON_VERTICES_M]
    out = []
    for point in coords_by_channel:
        key = tuple(round(float(v), 6) for v in point)
        out.append(vertices.index(key))
    return tuple(out)


def _synthetic_segments(
    true_perm: tuple[int, ...],
    azimuth_deg: float,
    n_segments: int,
    distance_m: float | None = 1.0,
    snr_db: float = 20.0,
    tonal: bool = False,
    seed: int = 7,
) -> list[np.ndarray]:
    """按 true_perm 生成多段 6ch 合成信号：(n, 6) float，单位约 ±1。"""
    rng = np.random.default_rng(seed)
    coords = np.asarray(_mod.HEXAGON_VERTICES_M, dtype=np.float64)[list(true_perm)]
    segments = []
    for _ in range(n_segments):
        n = N_SAMPLES
        t = np.arange(n) / RATE
        if tonal:
            # 准周期浊音（谐波落在带通 1–4 kHz 内 + 时变幅度调制）：严格平稳
            # 纯音会让逐窗 argmax 的误锁确定性锁死（各窗歧峰相同，中位数救不
            # 回）——真实语音是准周期，逐窗内容变化正是多窗口累积的作用前提。
            harmonics = (500.0, 1000.0, 2000.0, 3500.0)
            source = np.zeros(n)
            for k, freq in enumerate(harmonics):
                modulation = 1.0 + 0.6 * np.sin(2 * np.pi * (2.3 + 1.7 * k) * t)
                source += modulation * np.sin(2 * np.pi * freq * t + freq)
            source /= np.max(np.abs(source))
        else:
            source = rng.normal(0.0, 1.0, n)
            # 轻度低通留出宽带结构（1–4 kHz 带通后仍有足够能量），更接近真实语音
            kernel = np.ones(4) / 4.0
            source = np.convolve(source, kernel, mode="same")
        delays = _mod.arrival_delay_samples(coords, azimuth_deg, distance_m, RATE)
        channels = []
        for delay in delays:
            index = np.arange(n, dtype=np.float64) - delay  # delay>0 = 更晚到达
            channels.append(np.interp(index, np.arange(n, dtype=np.float64), source))
        matrix = np.stack(channels, axis=1)
        noise_sigma = 10 ** (-snr_db / 20.0)
        matrix = matrix + rng.normal(0.0, noise_sigma, matrix.shape)
        segments.append(matrix)
    return segments


class SyntheticSelfCheckTest(unittest.TestCase):
    def test_off_axis_recovers_unique_permutation(self):
        """斜 40° 入射：几何无对称简并 ⇒ 最优排列唯一且等于真值。"""
        true_perm = (3, 1, 4, 0, 5, 2)
        segments = _synthetic_segments(true_perm, azimuth_deg=40.0, n_segments=6)
        result = _mod.calibrate(segments, rate=RATE, azimuth_deg=40.0, distance_m=1.0)
        self.assertEqual(720, result["n_permutations"])
        self.assertEqual([list(true_perm)], result["best_permutations"])
        self.assertLess(result["median_residual_samples"], 0.5)
        self.assertTrue(result["criterion_pass"])

    def test_frontal_recovers_permutation_within_mirror_group(self):
        """正前方入射：几何关于 y 轴对称 ⇒ 最优组是 4 个镜像等价排列，必含真值。"""
        true_perm = (2, 5, 0, 4, 1, 3)
        segments = _synthetic_segments(true_perm, azimuth_deg=0.0, n_segments=6)
        result = _mod.calibrate(segments, rate=RATE, azimuth_deg=0.0, distance_m=1.0)
        self.assertIn(list(true_perm), result["best_permutations"])
        self.assertEqual(4, len(result["best_permutations"]))
        self.assertLess(result["median_residual_samples"], 0.5)

    def test_tonal_source_survives_via_multi_window_median(self):
        """准周期浊音：多窗口中位数纪律下不误锁、真值留在最优组内
        （对照 +54 样本失真案例）。

        只断言「不误锁 + 判据过」，不断言唯一：250–1000 Hz 的谐波带宽
        TDOA 分辨率 ≈1 样本，0.3 样本级顶点差物理上不可分辨——唯一性由
        宽带信号的斜入射用例覆盖。"""
        true_perm = (0, 1, 2, 3, 4, 5)
        segments = _synthetic_segments(
            true_perm, azimuth_deg=40.0, n_segments=4, tonal=True, snr_db=30.0
        )
        result = _mod.calibrate(segments, rate=RATE, azimuth_deg=40.0, distance_m=1.0)
        self.assertIn(list(true_perm), result["best_permutations"])
        self.assertLess(result["median_residual_samples"], 0.5)
        self.assertTrue(result["criterion_pass"])

    def test_stability_across_disjoint_subsets(self):
        """换一批段重跑：最优排列组不变（票 01 稳定性检查）。"""
        true_perm = (1, 4, 2, 0, 3, 5)
        first = _synthetic_segments(true_perm, azimuth_deg=25.0, n_segments=4, seed=11)
        second = _synthetic_segments(true_perm, azimuth_deg=25.0, n_segments=4, seed=12)
        result_a = _mod.calibrate(first, rate=RATE, azimuth_deg=25.0, distance_m=1.0)
        result_b = _mod.calibrate(second, rate=RATE, azimuth_deg=25.0, distance_m=1.0)
        self.assertEqual(result_a["best_permutations"], result_b["best_permutations"])


class CriterionGateTest(unittest.TestCase):
    def test_geometry_inconsistent_data_is_rejected_not_certified(self):
        """数据与任何通道↔顶点排列都不相容（各通道独立噪声，无共同声源）
        ⇒ 残差超限，不得给出结论（spec：不得强行取最优解）。

        注：排列自由度会吸收中等程度的方向错配（实测 60–150° 错配残差均
        <1 样本），所以「残差 <1」是映射正确的必要条件而非充分条件——
        方向假设本身要靠采集协议保证，此判据拦的是与几何结构性不相容。
        """
        rng = np.random.default_rng(3)
        segments = [rng.normal(0.0, 1.0, (N_SAMPLES, 6)) for _ in range(3)]
        result = _mod.calibrate(segments, rate=RATE, azimuth_deg=0.0)
        self.assertFalse(result["criterion_pass"])
        # 垃圾数据可能被 720 穷举凑出 <1 的中位残差（排列自由度太宽），
        # 真正拦住它的是排列无关的闭合差闸门。
        self.assertGreaterEqual(
            result["closure_residual_samples"], _mod.CLOSURE_CRITERION_SAMPLES
        )
        self.assertIsNone(result["certified_permutation"])

    def test_criterion_threshold_is_one_sample(self):
        """判据常量 = 1 样本（16 kHz 下 ≈21.4 mm），与 spec 预注册一致。"""
        self.assertEqual(1.0, _mod.RESIDUAL_CRITERION_SAMPLES)


class ExhaustivenessTest(unittest.TestCase):
    def test_all_720_permutations_scored(self):
        """穷举 6! = 720，不是采样或贪心。"""
        coords = np.asarray(_mod.HEXAGON_VERTICES_M)
        measured = {(i, j): 0.0 for i in range(6) for j in range(i + 1, 6)}
        table = _mod.score_all_permutations(measured, coords, azimuth_deg=0.0, rate=RATE)
        self.assertEqual(720, len(table))
        residuals = [item["median_residual"] for item in table]
        self.assertTrue(math.isfinite(min(residuals)))


if __name__ == "__main__":
    unittest.main()
