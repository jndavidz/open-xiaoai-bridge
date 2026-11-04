"""主链降声道器的构造接线（lx06-beamform-8ch 票 01）。

seam = `XiaoAI._apply_audio_input_config()` + `XiaoAI.audio_health()`：
配置里的 `channel_strategy` 是波束值时，几何必须来自标定表（不许静默用兼容
默认几何），构建失败必须回落 strongest 且回落可见。断言面 = XiaoAI 的健康字段
与 `ChannelDownmixer` 自身的公开属性（`strategy` / `beam_coords_m` /
`beamformer` / 输出长度），不伸进波束器内部的延迟表。

`open_xiaoai_server` 用桩替换（与 tests/test_health.py 同口径），不连真机。
"""

import os
import sys
import types
import unittest
import warnings
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("open_xiaoai_server", types.ModuleType("open_xiaoai_server"))

from core.services.audio.channel_map import load_channel_map  # noqa: E402
from core.xiaoai import XiaoAI  # noqa: E402


class FakeConfigManager:
    """只实现 `get_app_config("a.b.c", default)` 的点查路径。"""

    def __init__(self, data: dict):
        self._data = data

    def get_app_config(self, key: str, default=None):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


AUDIO_KEYS = (
    "_audio_channels",
    "_audio_bits",
    "_channel_strategy",
    "_fixed_channel",
    "_reference_channels",
    "_downmixer",
    "_ch0_downmixer",
    "_wakeup_chain",
    "_current_channel",
    "_downmixer_fallbacks",
)


class DownmixerWiringTest(unittest.TestCase):
    def setUp(self):
        self._saved = {key: getattr(XiaoAI, key) for key in AUDIO_KEYS}
        self._saved_config = XiaoAI.config_manager
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("AUDIO_INPUT_CHANNELS", "AUDIO_INPUT_BITS")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(XiaoAI, key, value)
        XiaoAI.config_manager = self._saved_config
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _apply(self, **audio_input):
        XiaoAI.config_manager = FakeConfigManager({"audio_input": audio_input})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            XiaoAI._apply_audio_input_config()
        return caught

    def test_beamform_strategy_gets_calibrated_geometry(self):
        """8ch + 波束：坐标来自标定表，构造期不得出现「退回兼容默认几何」告警。"""
        caught = self._apply(
            channels=8,
            bits_per_sample=32,
            channel_strategy="beamform_fixed",
            reference_channels=[6, 7],
        )
        self.assertIsNotNone(XiaoAI._downmixer)
        self.assertEqual("beamform_fixed", XiaoAI._downmixer.strategy)
        self.assertEqual(
            load_channel_map()["channel_coords_m"], list(XiaoAI._downmixer.beam_coords_m)
        )
        self.assertEqual(
            [],
            [str(w.message) for w in caught if "兼容默认几何" in str(w.message)],
            "标定坐标没注入主链，波束会静默用错几何",
        )

    def test_non_beamform_strategy_still_byte_identical(self):
        """strongest（现产默认）：不注入坐标、不产生波束告警、策略名不变。"""
        caught = self._apply(
            channels=8, bits_per_sample=32, channel_strategy="strongest",
            reference_channels=[6, 7],
        )
        self.assertEqual("strongest", XiaoAI._downmixer.strategy)
        self.assertIsNone(XiaoAI._downmixer.beamformer)
        self.assertEqual([], [str(w.message) for w in caught])

    def test_bad_beamform_config_falls_back_to_strongest(self):
        """8ch 忘声明参考声道 ⇒ 波束构建失败 ⇒ 回落 strongest 并计数可见。"""
        self._apply(channels=8, bits_per_sample=32, channel_strategy="beamform_fixed")
        self.assertEqual("strongest", XiaoAI._downmixer.strategy)
        self.assertEqual(1, XiaoAI._downmixer_fallbacks)
        health = XiaoAI.audio_health()
        self.assertEqual("beamform_fixed", health["channel_strategy"])
        self.assertEqual("strongest", health["channel_strategy_applied"])

    def test_healthy_beamform_reports_applied_strategy(self):
        self._apply(
            channels=8, bits_per_sample=32, channel_strategy="beamform_fixed",
            reference_channels=[6, 7],
        )
        health = XiaoAI.audio_health()
        self.assertEqual("beamform_fixed", health["channel_strategy_applied"])
        self.assertEqual(0, health["downmixer_fallbacks"])
        self.assertIsNone(health["current_channel"], "波束下 current_channel 应为空")

    def test_beamform_output_length_matches_mono_stream(self):
        """回落/主链都必须按 8ch 帧产出 1ch：1 秒音频 = 16000 个 int16。"""
        self._apply(
            channels=8, bits_per_sample=32, channel_strategy="beamform_fixed",
            reference_channels=[6, 7],
        )
        frames = np.zeros((16000, 8), dtype=np.int16)
        out = XiaoAI._downmixer.feed(frames.tobytes())
        self.assertEqual(16000 * 2, len(out))


if __name__ == "__main__":
    unittest.main()
