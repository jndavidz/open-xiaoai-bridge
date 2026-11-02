"""播放期回声消除（music-far spec 票 04）的行为测试。

只测 public seam：`EchoCanceller.feed()` / `stats()` / `reset()`。
合成 far/near 对（已知时延 + 已知幅度），断言：
  1. 收敛后 ERLE ≥ 10 dB（票 04 验收线，离线实测 14.3 dB 的保守下界）；
  2. 三种旁路（参考全零 / 参考恒定 / 双讲）输出与输入逐样本一致（直通）；
  3. 发散守卫：回声路径突变触发重置 + 冷却直通，随后重新收敛；
  4. 极性反转与 64 样本参考超前（3.9–4.1 ms 实测对齐窗）被正确处理。
不依赖 sherpa/模型/设备。
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.aec import EchoCanceller  # noqa: E402

RATE = 16000
CHANNELS = 8  # 6 麦 + ch6/ch7 播放参考（spec：ch6 与 ch7 逐样本相同）
REF_CHANNEL = 6
MIC_CHANNEL = 0
REF_LEAD = 64  # 参考领先 4.0 ms
FEED_BYTES = 1440 * CHANNELS * 2  # 与生产 chunk 同宽（1440 样本）


def _far_end(n: int, seed: int = 5) -> np.ndarray:
    """类音乐 far-end：多谐波 + 少量噪声，幅值 ~0.5 FS。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / RATE
    signal = np.zeros(n)
    for freq in (220.0, 440.0, 987.0, 1567.0):
        signal += np.sin(2 * np.pi * freq * t + freq * 0.13)
    signal /= np.max(np.abs(signal))
    signal += 0.01 * rng.standard_normal(n)
    return 0.5 * signal


def _interleaved(mic: np.ndarray, ref: np.ndarray, total_samples: int) -> bytes:
    """ch0=mic、ch6=ref，其余声道置零，组帧为交错 int16 PCM。

    mic / ref 均为 float [-1, 1] 域，此处统一定标到 int16（此前 ref 忘乘
    32768，整路参考转 int16 后全 0，被模块按 ref_zero 旁路——测试全废）。
    """
    block = np.zeros((total_samples, CHANNELS), dtype=np.int16)
    block[:, MIC_CHANNEL] = np.clip(mic * 32768, -32768, 32767).astype(np.int16)
    block[:, REF_CHANNEL] = np.clip(ref * 32768, -32768, 32767).astype(np.int16)
    return block.tobytes()


def _feed_all(aec: EchoCanceller, mic: np.ndarray, ref: np.ndarray) -> bytes:
    pcm = _interleaved(mic, ref, len(mic))
    out = bytearray()
    for offset in range(0, len(pcm), FEED_BYTES):
        out += aec.feed(pcm[offset : offset + FEED_BYTES])
    return bytes(out)


def _erle_db(mic: np.ndarray, output_pcm: bytes, tail_samples: int) -> float:
    """末段输出功率相对输入功率的抑制量（dB）。mic 为 float [-1,1] 域。"""
    out = np.frombuffer(output_pcm, dtype=np.int16).astype(np.float64) / 32768.0
    d = mic[-tail_samples:]
    e = out[-tail_samples:]
    return 10 * np.log10(float(np.mean(d**2)) / (float(np.mean(e**2)) + 1e-12))


def _to_int16(x: np.ndarray) -> np.ndarray:
    """float [-1,1] 域 → int16 量纲 int64（与 _interleaved 同一定标）。"""
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int64)


ECHO_GAIN = 0.4  # 物理合理耦合（< 0.5：Geigel 检测的原理性上界，见 aec.py 注）


def _echo(ref: np.ndarray, lead: int = REF_LEAD) -> np.ndarray:
    """麦克风回声 = ECHO_GAIN × 参考延迟 lead 样本。"""
    return ECHO_GAIN * np.roll(ref, lead)


class ConvergenceTest(unittest.TestCase):
    def test_erle_reaches_threshold_after_convergence(self):
        """合成验收：已知时延 + 已知幅度下，收敛后 ERLE ≥ 10 dB。"""
        n = RATE * 2
        rng = np.random.default_rng(42)
        ref_raw = _far_end(n)
        echo = _echo(ref_raw)
        near = 0.002 * rng.standard_normal(n)
        mic = echo + near
        aec = EchoCanceller(
            channels=CHANNELS,
            ref_channel=REF_CHANNEL,
            mic_channel=MIC_CHANNEL,
            ref_lead_samples=REF_LEAD,
            ref_polarity_inverted=True,
        )
        _feed_all(aec, np.zeros(RATE), ref_raw[:RATE])  # 1s 收敛期
        tail = _feed_all(aec, mic[RATE:], ref_raw[RATE:])
        self.assertGreaterEqual(
            _erle_db(mic[RATE:], tail, RATE // 2),
            10.0,
            "收敛后 ERLE 未达 10 dB 验收线",
        )
        stats = aec.stats()
        self.assertLess(
            stats["bypass_double_talk"],
            5,
            f"双讲误判过多（{stats['bypass_double_talk']} 块）说明 Geigel 阈值过紧",
        )
        self.assertAlmostEqual(
            aec.stats()["echo_gain"], -ECHO_GAIN, places=2,
            msg="收敛增益应逼近真实回声路径（含极性符号）",
        )

    def test_positive_polarity_reference_also_converges(self):
        """极性反转由 NLMS 自学符号：非反转参考同样收敛（设备事实仅记录在案）。"""
        n = RATE
        ref_raw = _far_end(n, seed=9)
        echo = _echo(ref_raw)
        aec = EchoCanceller(
            channels=CHANNELS, ref_channel=REF_CHANNEL, ref_lead_samples=REF_LEAD,
            ref_polarity_inverted=False,
        )
        _feed_all(aec, np.zeros(RATE // 2), ref_raw[: RATE // 2])
        tail = _feed_all(aec, echo[RATE // 2 :], ref_raw[RATE // 2 :])
        self.assertGreaterEqual(
            _erle_db(echo[RATE // 2 :], tail, RATE // 4), 10.0
        )


class BypassTest(unittest.TestCase):
    def _aec(self) -> EchoCanceller:
        return EchoCanceller(
            channels=CHANNELS, ref_channel=REF_CHANNEL, ref_lead_samples=REF_LEAD
        )

    def _mic_channel(self, pcm: bytes) -> np.ndarray:
        # AEC 输出是单声道（每输入样本一个输出样本），不再按声道重排
        return np.frombuffer(pcm, dtype=np.int16).astype(np.int64)

    def test_ref_all_zero_passthrough_is_sample_exact(self):
        """参考全零 → 直通：输出与输入逐样本一致。"""
        n = 2000
        rng = np.random.default_rng(1)
        mic = 0.3 * rng.standard_normal(n)
        aec = self._aec()
        out = _feed_all(aec, mic, np.zeros(n))
        np.testing.assert_array_equal(self._mic_channel(out), _to_int16(mic))
        stats = aec.stats()
        self.assertGreater(stats["bypass_ref_zero"], 0)
        self.assertEqual(stats["bypass_ref_constant"], 0)

    def test_ref_constant_passthrough(self):
        """参考恒定（非零）无信息 → 直通并单独计数。"""
        n = 2000
        rng = np.random.default_rng(2)
        mic = 0.3 * rng.standard_normal(n)
        aec = self._aec()
        out = _feed_all(aec, mic, np.full(n, 777 / 32768.0))
        np.testing.assert_array_equal(self._mic_channel(out), _to_int16(mic))
        self.assertGreater(aec.stats()["bypass_ref_constant"], 0)

    def test_double_talk_passthrough(self):
        """双讲（近端幅值压过参考峰值）→ 直通，不动滤波器。"""
        n = RATE
        rng = np.random.default_rng(3)
        ref_raw = _far_end(n, seed=11)
        echo = _echo(ref_raw)
        near = 0.9 * np.sin(2 * np.pi * 90.0 * np.arange(n) / RATE)  # 响亮近端
        mic = echo + near
        aec = self._aec()
        _feed_all(aec, np.zeros(RATE // 2), ref_raw[: RATE // 2])
        out = _feed_all(aec, mic[RATE // 2 :], ref_raw[RATE // 2 :])
        self.assertGreater(aec.stats()["bypass_double_talk"], 0)
        # 双讲块必须原样输出（直通）
        np.testing.assert_array_equal(self._mic_channel(out), _to_int16(mic[RATE // 2 :]))


class DivergenceGuardTest(unittest.TestCase):
    def test_gain_corruption_triggers_divergence_bypass_and_recovery(self):
        """增益被污染（数据流错位/系数异常的等价物）→ 守卫重置 + 冷却直通 → 重新收敛。

        注：不用「路径符号翻转」做发散 trigger——单抽头 NLMS 对符号翻转
        4 块内自愈（见下一个用例），快过 EMA（τ≈0.2s），守卫来不及触发。
        """
        n = RATE
        ref_raw = _far_end(n, seed=13)
        echo = _echo(ref_raw)
        aec = EchoCanceller(
            channels=CHANNELS, ref_channel=REF_CHANNEL, ref_lead_samples=REF_LEAD
        )
        _feed_all(aec, echo[: RATE // 2], ref_raw[: RATE // 2])  # 收敛
        self.assertGreaterEqual(aec.stats()["erle_db"] or 0.0, 10.0)

        # 白盒注入：增益跑飞到 75 倍真值（|e|≈30·|x| ⇒ 输出功率 ~900× 输入，
        # EMA 首块即越限）。注：小幅度污染（如 -3）会被 NLMS 在数块内自愈，
        # 快过 EMA（τ≈0.2s），守卫本来就不该为它触发——那是自愈路径的用例。
        aec._gain = -30.0
        _feed_all(aec, echo, ref_raw)
        stats = aec.stats()
        self.assertGreaterEqual(stats["divergence_resets"], 1, "增益污染应触发发散重置")
        self.assertGreaterEqual(stats["bypass_divergence"], 1, "发散/冷却期应直通")
        # 冷却结束后重新收敛
        tail = _feed_all(aec, echo, ref_raw)
        self.assertGreaterEqual(_erle_db(echo, tail, RATE // 2), 10.0)

    def test_polarity_flip_self_heals_without_bypass(self):
        """回声路径符号翻转 → 单抽头 NLMS 数块内自愈，无需发散旁路。"""
        n = RATE
        ref_raw = _far_end(n, seed=17)
        aec = EchoCanceller(
            channels=CHANNELS, ref_channel=REF_CHANNEL, ref_lead_samples=REF_LEAD
        )
        _feed_all(aec, _echo(ref_raw)[: RATE // 2], ref_raw[: RATE // 2])
        flipped = -_echo(ref_raw)  # 路径突变：符号翻转
        tail = _feed_all(aec, flipped, ref_raw)
        stats = aec.stats()
        self.assertGreaterEqual(
            _erle_db(flipped, tail, RATE // 4), 10.0, "符号翻转后应自愈"
        )
        self.assertEqual(
            stats["divergence_resets"], 0, "自愈路径不应触发发散重置"
        )


class ContractTest(unittest.TestCase):
    def test_default_off_is_upstream_decision_module_itself_is_explicit(self):
        """模块无隐式全局单例：构造即生效、reset 回初始态（默认关闭由上游管）。"""
        aec = EchoCanceller(channels=CHANNELS, ref_channel=REF_CHANNEL)
        stats = aec.stats()
        self.assertEqual(stats["frames_in"], 0)
        self.assertEqual(stats["latency_ms"], 0.0, "零前瞻：不得引入帧积压")
        aec.feed(np.zeros((100, CHANNELS), dtype=np.int16).tobytes())
        aec.reset()
        self.assertEqual(aec.stats()["frames_in"], 0)

    def test_constructor_validation(self):
        with self.assertRaises(ValueError):
            EchoCanceller(channels=1)
        with self.assertRaises(ValueError):
            EchoCanceller(channels=8, ref_channel=8)
        with self.assertRaises(ValueError):
            EchoCanceller(channels=8, mic_channel=8)
        with self.assertRaises(ValueError):
            EchoCanceller(channels=8, ref_channel=0, mic_channel=0)

    def test_short_and_unaligned_input_ignored(self):
        aec = EchoCanceller(channels=CHANNELS, ref_channel=REF_CHANNEL)
        self.assertEqual(aec.feed(b""), b"")
        self.assertEqual(aec.feed(b"\x01\x02"), b"")  # 不足一个声道帧
        # 尾部不满一个多声道帧的字节被忽略，不产生错误输出
        out = aec.feed(np.zeros((10, CHANNELS), dtype=np.int16).tobytes() + b"\x01")
        self.assertEqual(len(out), 10 * 2)


if __name__ == "__main__":
    unittest.main()
