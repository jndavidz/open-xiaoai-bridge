"""唤醒输入链 sidecar 模式（lx06-audio-sidecar spec 票 02）的行为测试。

只测外部行为：sidecar 模式分派、失败回落（未就绪/断流/帧非法）、
近端与参考同源的硬约束（client 流在 sidecar 有效态不投递）、统计字段。
不连设备、不跑 SSH（采集器侧见 test_audio_sidecar.py）。
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio import wakeup_input as wi  # noqa: E402
from core.services.audio.wakeup_input import (  # noqa: E402
    MODE_INHERIT,
    MODE_SIDECAR,
    WakeupInputChain,
    is_chain_active,
    parse_wakeup_channel_config,
)

CLIENT_CH = 6   # client 采集 6ch（sidecar 与它无关，spec 前提）
SIDECAR_CH = 8
RATE = 16000


def _client_pcm(n: int, amp: float = 8000.0) -> bytes:
    """client 6ch 交错帧：ch0 载响亮音乐（模拟播放串音），无参考。"""
    t = np.arange(n) / RATE
    block = np.zeros((n, CLIENT_CH), dtype=np.int16)
    block[:, 0] = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    return block.tobytes()


def _sidecar_pcm(n: int, ref_amp: float = 6000.0) -> bytes:
    """sidecar 8ch 交错帧：ch0 近端人声 + ch6 播放参考（同一次采集）。"""
    t = np.arange(n) / RATE
    block = np.zeros((n, SIDECAR_CH), dtype=np.int16)
    block[:, 0] = (8000 * np.sin(2 * np.pi * 300 * t)).astype(np.int16)
    block[:, 6] = (ref_amp * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    block[:, 7] = block[:, 6]  # ch6 ≡ ch7（事实层 §11）
    return block.tobytes()


def _sidecar_config(**overrides) -> dict:
    cfg = {
        "wakeup_channel": {"playing": "sidecar", "idle": "strongest",
                           "fixed_channel": 0},
        "sidecar": {"enabled": True, "host": "10.10.10.20",
                    "key_path": "/keys/lx06"},
        "specsub": {"enabled": False, "alpha": 0.6, "floor_db": -20.0,
                    "ref_lead_samples": 64},
        "reference_channels": [6, 7],
    }
    cfg.update(overrides)
    return cfg


class ModeParseTests(unittest.TestCase):
    def test_sidecar_is_valid_mode(self):
        parsed = parse_wakeup_channel_config({"playing": "sidecar"})
        self.assertEqual(parsed["playing"], MODE_SIDECAR)

    def test_invalid_mode_still_falls_back(self):
        with self.assertLogs("xiaozhi", level="WARNING"):
            parsed = parse_wakeup_channel_config({"playing": "nope"})
        self.assertEqual(parsed["playing"], MODE_INHERIT)

    def test_chain_active_with_sidecar_regardless_of_client_channels(self):
        cfg = _sidecar_config()
        self.assertTrue(is_chain_active(cfg["wakeup_channel"], CLIENT_CH))


class DispatchTests(unittest.TestCase):
    def _chain(self, cfg=None) -> WakeupInputChain:
        return WakeupInputChain(cfg or _sidecar_config(), CLIENT_CH)

    def test_sidecar_specsub_built_against_8ch(self):
        chain = self._chain()
        self.assertIsNotNone(chain._sidecar_specsub)
        self.assertEqual(chain._sidecar_specsub.channels, SIDECAR_CH)
        self.assertEqual(chain._sidecar_specsub.ref_channel, 6)
        # specsub.enabled=False 不影响 sidecar 模式（模式本身即开关），但
        # 调优参数被沿用
        self.assertEqual(chain._sidecar_specsub.alpha, 0.6)

    def test_not_ready_falls_back_to_strongest(self):
        chain = self._chain()
        self.assertFalse(chain._sidecar_ready())
        # 未就绪：client 6ch 走 strongest 选麦，产出非空
        out = chain.feed(_client_pcm(1600))
        self.assertGreater(len(out), 0)
        self.assertEqual(len(out) % 2, 0)
        # sidecar 帧未就绪不投递
        self.assertEqual(chain.feed_sidecar(_sidecar_pcm(1600)), b"")

    def test_healthy_client_stream_not_fed_and_sidecar_supplies(self):
        """硬约束：sidecar 有效期 client 流不投递（禁止跨流拼接）。"""
        chain = self._chain()
        chain.set_playing(True)
        chain.set_sidecar_health(True)
        self.assertTrue(chain._sidecar_ready())
        # client 6ch 响亮音乐进不来（哪怕 reference_channels 未含它）
        self.assertEqual(chain.feed(_client_pcm(1600)), b"")
        # sidecar 8ch 正常产出谱减 1ch
        out = chain.feed_sidecar(_sidecar_pcm(1600))
        self.assertGreater(len(out), 0)
        self.assertEqual(len(out) % 2, 0)
        self.assertEqual(chain._counters["sidecar_frames"], 1)

    def test_health_flip_falls_back_and_counts(self):
        chain = self._chain()
        chain.set_playing(True)
        chain.set_sidecar_health(True)
        self.assertEqual(chain.feed(_client_pcm(1600)), b"")
        with self.assertLogs("xiaozhi", level="WARNING"):
            chain.set_sidecar_health(False)
        # 回落 strongest：client 流重新供给，输出非空
        out = chain.feed(_client_pcm(1600))
        self.assertGreater(len(out), 0)
        stats = chain.stats()
        self.assertEqual(stats["sidecar"]["fallbacks"], 1)
        # 恢复后再断一次：计数累加
        chain.set_sidecar_health(True)
        with self.assertLogs("xiaozhi", level="WARNING"):
            chain.set_sidecar_health(False)
        self.assertEqual(chain.stats()["sidecar"]["fallbacks"], 2)

    def test_stale_health_falls_back(self):
        """健康标记过期（宿主未续期）⇒ 自动回落，不悬挂在陈旧健康上。"""
        chain = self._chain()
        chain.set_playing(True)
        chain.set_sidecar_health(True)
        self.assertEqual(chain.feed(_client_pcm(1600)), b"")
        future = chain._sidecar_health_at + chain._sidecar_stale_window + 1.0
        with mock.patch.object(wi.time, "monotonic", return_value=future):
            out = chain.feed(_client_pcm(1600))
        self.assertGreater(len(out), 0)  # 回落 strongest，未悬挂

    def test_invalid_frame_dropped_and_counted(self):
        chain = self._chain()
        chain.set_playing(True)
        chain.set_sidecar_health(True)
        # 帧长非 8ch/S16 帧字节数（16B）整数倍 ⇒ 丢弃并计数，不抛错
        bad = b"\x00" * (16 * 3 + 5)
        self.assertEqual(chain.feed_sidecar(bad), b"")
        self.assertEqual(chain._counters["sidecar_invalid_frames"], 1)
        self.assertEqual(chain._counters["sidecar_frames"], 0)

    def test_no_sidecar_section_still_falls_back(self):
        """playing=sidecar 但 sidecar 段缺失 ⇒ 不抛错，回落 strongest。"""
        cfg = _sidecar_config()
        del cfg["sidecar"]
        chain = WakeupInputChain(cfg, CLIENT_CH)
        self.assertIsNotNone(chain._sidecar_specsub)  # 谱减器按默认 8ch 构建
        self.assertFalse(chain._sidecar_ready())
        out = chain.feed(_client_pcm(1600))
        self.assertGreater(len(out), 0)

    def test_inherit_config_zero_change(self):
        """缺省双 inherit：零行为变化，无 sidecar 附件。"""
        chain = WakeupInputChain({}, CLIENT_CH)
        self.assertFalse(chain.active)
        self.assertIsNone(chain._sidecar_specsub)
        self.assertEqual(chain.feed(_client_pcm(1600)), b"")
        self.assertNotIn("sidecar", chain.stats())

    def test_stats_sidecar_section_semantics(self):
        chain = self._chain()
        chain.set_playing(True)
        chain.set_sidecar_health(True)
        chain.feed_sidecar(_sidecar_pcm(1600))
        sc = chain.stats()["sidecar"]
        self.assertTrue(sc["configured"])
        self.assertTrue(sc["healthy"])
        self.assertTrue(sc["ready"])
        self.assertEqual(sc["channels"], SIDECAR_CH)
        self.assertEqual(sc["fallbacks"], 0)

    def test_reset_keeps_counters_and_processing_continues(self):
        chain = self._chain()
        chain.set_playing(True)
        chain.set_sidecar_health(True)
        chain.feed_sidecar(_sidecar_pcm(1600))
        chain.reset()
        # 计数是生命周期口径不随 reset 清零（同既有 aec/mode_switches 语义）；
        # 谱减器内部对齐线/OLA 状态已重置（reset 后可继续喂帧，不抛错）
        self.assertEqual(chain._counters["sidecar_frames"], 1)
        self.assertEqual(len(chain.feed_sidecar(_sidecar_pcm(1600))) % 2, 0)


if __name__ == "__main__":
    unittest.main()
