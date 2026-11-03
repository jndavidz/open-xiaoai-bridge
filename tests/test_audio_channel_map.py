"""LX06 通道映射表资产与加载器（lx06-mic-array-frontend 票 01→02 交接件）。

映射表是票 01 标定的数据产物（非代码常量）：波束与未来任何多麦模块都从
这里拿通道几何。加载器纯 stdlib。
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.channel_map import (  # noqa: E402
    DEFAULT_CHANNEL_MAP_PATH,
    load_channel_map,
)

# 票 01 标定结论（channel-map.json 证据件）：ch0 从右下顶点起逆时针连续。
CALIBRATED_PERM = (2, 1, 0, 5, 4, 3)


class ChannelMapAssetTest(unittest.TestCase):
    def test_default_asset_exists_and_loads(self):
        self.assertTrue(DEFAULT_CHANNEL_MAP_PATH.is_file())
        channel_map = load_channel_map()
        self.assertEqual("lx06-channel-map/1", channel_map["schema"])
        self.assertEqual(6, len(channel_map["channel_coords_m"]))

    def test_coords_match_calibrated_permutation(self):
        """资产里的逐通道坐标 = 六边形顶点表按标定排列取值。"""
        channel_map = load_channel_map()
        vertices = [tuple(v) for v in channel_map["hexagon_vertices_m"]]
        expected = [vertices[i] for i in CALIBRATED_PERM]
        actual = [tuple(c) for c in channel_map["channel_coords_m"]]
        self.assertEqual(expected, actual)
        self.assertEqual(list(CALIBRATED_PERM), channel_map["channel_to_vertex"])

    def test_coords_form_regular_hexagon(self):
        """六点到中心等距 3.5 cm、相邻 60°（几何自检，不依赖票面数字）。"""
        import math

        channel_map = load_channel_map()
        radii = [math.hypot(x, y) for x, y in channel_map["channel_coords_m"]]
        for radius in radii:
            self.assertAlmostEqual(0.035, radius, places=6)
        angles = sorted(math.atan2(y, x) for x, y in channel_map["channel_coords_m"])
        for a, b in zip(angles, angles[1:]):
            # 资产坐标经 6 位小数舍入，角度公差放宽到 1e-5 rad（≈0.06 mm 弦差）
            self.assertAlmostEqual(math.pi / 3, b - a, places=5)

    def test_provenance_records_calibration_evidence(self):
        channel_map = load_channel_map()
        provenance = channel_map["provenance"]
        self.assertIn("median_residual_samples", provenance)
        self.assertLess(provenance["median_residual_samples"], 1.0)

    def test_load_rejects_structurally_wrong_map(self):
        bad = {"schema": "lx06-channel-map/1", "channel_coords_m": [[0, 0]] * 5}
        with self.assertRaises(ValueError):
            load_channel_map(data=bad)


if __name__ == "__main__":
    unittest.main()
