"""按播放态选择唤醒输入通道（music-far spec 票 05）的行为测试。

只测外部行为：WakeupInputChain 的模式选择/回落、AEC 旁路直通、独立链与
主链的隔离（源级契约）、KWS 输入流解析。不连设备、不加载模型。
"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio import wakeup_input as wi  # noqa: E402
from core.services.audio.wakeup_input import (  # noqa: E402
    MODE_AEC,
    MODE_FIXED,
    MODE_INHERIT,
    MODE_STRONGEST,
    WakeupInputChain,
    is_chain_active,
    parse_wakeup_channel_config,
)

CHANNELS = 8
RATE = 16000


def _pcm(mic: np.ndarray, ref: np.ndarray | None = None, channels: int = CHANNELS) -> bytes:
    block = np.zeros((len(mic), channels), dtype=np.int16)
    block[:, 0] = np.clip(mic, -32768, 32767).astype(np.int16)
    if ref is not None:
        block[:, 6] = np.clip(ref, -32768, 32767).astype(np.int16)
    return block.tobytes()


def _tone(freq: float, n: int, amp: float = 8000.0) -> np.ndarray:
    t = np.arange(n) / RATE
    return amp * np.sin(2 * np.pi * freq * t)


class ParseConfigTest(unittest.TestCase):
    def test_default_is_inherit(self):
        parsed = parse_wakeup_channel_config(None)
        self.assertEqual(parsed["playing"], MODE_INHERIT)
        self.assertEqual(parsed["idle"], MODE_INHERIT)

    def test_invalid_mode_warns_and_falls_back(self):
        with self.assertLogs("xiaozhi", level="WARNING") as captured:
            parsed = parse_wakeup_channel_config({"playing": "beam", "idle": "aec"})
        self.assertEqual(parsed["playing"], MODE_INHERIT)
        self.assertEqual(parsed["idle"], MODE_AEC)
        self.assertTrue(any("playing" in line for line in captured.output))

    def test_chain_activity(self):
        self.assertFalse(is_chain_active(None, 6))
        self.assertFalse(is_chain_active({"playing": "inherit", "idle": "inherit"}, 6))
        self.assertTrue(is_chain_active({"playing": "aec"}, 8))
        self.assertTrue(is_chain_active({"idle": "strongest"}, 6))
        self.assertFalse(is_chain_active({"playing": "strongest"}, 1), "单声道恒不启用")


class ChainSelectionTest(unittest.TestCase):
    def test_playing_and_idle_use_different_sources(self):
        chain = WakeupInputChain(
            {"wakeup_channel": {"playing": "fixed", "idle": "strongest"}}, CHANNELS
        )
        self.assertTrue(chain.active)
        n = 1600
        # ch3 最响：空闲期 strongest 应选 ch3
        block = np.zeros((n, CHANNELS), dtype=np.int16)
        block[:, 0] = _tone(440.0, n, 2000).astype(np.int16)
        block[:, 3] = _tone(440.0, n, 9000).astype(np.int16)
        raw = block.tobytes()
        chain.set_playing(False)
        for _ in range(12):
            chain.feed(raw)
        self.assertEqual(chain._mixers[MODE_STRONGEST].current_channel, 3)
        # 播放期切 fixed：输出 = ch0 逐样本
        chain.set_playing(True)
        out = chain.feed(raw)
        np.testing.assert_array_equal(
            np.frombuffer(out, dtype=np.int16), block[:, 0]
        )

    def test_aec_mode_passthrough_when_reference_absent(self):
        """aec 模式 + 参考全零 → AEC 旁路直通（mic_channel 逐样本原样）。"""
        chain = WakeupInputChain(
            {"wakeup_channel": {"playing": "aec"}, "aec": {"enabled": True}},
            CHANNELS,
        )
        chain.set_playing(True)
        mic = _tone(300.0, 1600, 4000)
        out = chain.feed(_pcm(mic, np.zeros(1600)))
        np.testing.assert_array_equal(np.frombuffer(out, dtype=np.int16), mic.astype(np.int16))

    def test_aec_mode_without_reference_channel_falls_back_to_fixed(self):
        """6ch 采集（无参考声道）配置 aec → 回落 fixed（不静默：告警）。"""
        with self.assertLogs("xiaozhi", level="WARNING"):
            chain = WakeupInputChain(
                {"wakeup_channel": {"playing": "aec"}, "aec": {"enabled": True}}, 6
            )
        chain.set_playing(True)
        mic = _tone(300.0, 1600, 4000)
        out = chain.feed(_pcm(mic, channels=6))
        np.testing.assert_array_equal(np.frombuffer(out, dtype=np.int16), mic.astype(np.int16))
        self.assertEqual(chain._last_mode, MODE_FIXED)

    def test_mode_switch_counted_and_output_tracks_state(self):
        chain = WakeupInputChain(
            {"wakeup_channel": {"playing": "fixed", "idle": "strongest"}}, CHANNELS
        )
        n = 1600
        mic = _tone(300.0, n, 4000)
        raw = _pcm(mic)
        chain.set_playing(False)
        chain.feed(raw)  # idle → strongest
        chain.set_playing(True)
        chain.feed(raw)  # playing → fixed（切换 +1）
        self.assertGreaterEqual(chain.stats()["mode_switches"], 1)

    def test_inactive_chain_feeds_nothing(self):
        chain = WakeupInputChain({}, CHANNELS)
        self.assertFalse(chain.active)
        self.assertEqual(chain.feed(_pcm(_tone(300.0, 100))), b"")


class AecOutputTest(unittest.TestCase):
    def test_aec_mode_cancels_known_echo(self):
        """播放期 aec 模式端到端：合成回声被消除（输出功率显著下降）。"""
        chain = WakeupInputChain(
            {"wakeup_channel": {"playing": "aec"}, "aec": {"enabled": True}},
            CHANNELS,
        )
        chain.set_playing(True)
        ref = _tone(220.0, RATE, 12000.0) + _tone(660.0, RATE, 6000.0)
        echo = 0.5 * np.roll(ref, 64)
        mic = echo + 0.002 * np.random.default_rng(1).standard_normal(RATE) * 32768
        raw = _pcm(mic, ref)
        out = bytearray()
        chunk = 1440 * CHANNELS * 2
        for off in range(0, len(raw), chunk):
            out += chain.feed(raw[off : off + chunk])
        out = np.frombuffer(bytes(out), dtype=np.int16).astype(np.float64)
        d_in = np.clip(mic, -32768, 32767).astype(np.float64)
        tail = slice(-RATE // 2, None)
        erle = 10 * np.log10(
            np.mean(d_in[tail] ** 2) / (np.mean(out[tail] ** 2) + 1e-9)
        )
        self.assertGreaterEqual(erle, 10.0, "播放期 aec 链未达到 10 dB 消除")
        stats = chain.stats()["aec"]
        self.assertEqual(stats["latency_ms"], 0.0)


class KwsStreamResolutionTest(unittest.TestCase):
    """KWS 输入流源解析：链激活 → GlobalStreamKws；否则 None（主链默认）。"""

    def test_resolution_contract_in_source(self):
        """源级契约：start() 经 _resolve_input_stream 选源，active 判定共用。"""
        source = (ROOT / "core/services/audio/kws/__init__.py").read_text(encoding="utf8")
        self.assertIn("GlobalStreamKws", source)
        self.assertIn("is_chain_active", source)
        self.assertIn("source=source", source)
        # xiaoai 侧：链喂 GlobalStreamKws，主链 GlobalStream.input 不受影响
        xiaoai_source = (ROOT / "core/xiaoai.py").read_text(encoding="utf8")
        self.assertIn("GlobalStreamKws.input(kws_pcm)", xiaoai_source)
        chain_feed_index = xiaoai_source.index("chain.feed(raw)")
        global_input_index = xiaoai_source.index("GlobalStream.input(pcm)")
        self.assertLess(chain_feed_index, global_input_index)
        self.assertIn(
            "GlobalStreamCh0.input(ch0)",
            xiaoai_source,
            "ch0 影子臂喂入不应被本改动触碰",
        )

    def test_is_chain_active_is_the_shared_predicate(self):
        """两侧（KWS 解析 / xiaoai 链构建）用同一判定函数，防口径漂移。"""
        self.assertTrue(wi.is_chain_active({"playing": "strongest"}, 6))
        self.assertFalse(wi.is_chain_active({}, 6))


if __name__ == "__main__":
    unittest.main()
