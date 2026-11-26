# -*- coding: utf-8 -*-
"""cch_config 单一实现守门（P5：_cfg 三份归并）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CchConfigTest(unittest.TestCase):
    def test_cfg_reads_song_request_config(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from core.music import cch_config

        # 无 override 时返回 default（与 sr._cfg 同语义）
        self.assertEqual("d", cch_config.cfg("nonexistent_key_p5", "d"))

    def test_three_cch_modules_share_single_cfg(self):
        """cch_cast/cch_click/cch_fav_sync 不得各自定义 _cfg（归并守门）。"""
        for rel in ("core/music/cch_cast.py", "core/music/cch_click.py",
                    "core/music/cch_fav_sync.py"):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("def _cfg(", src,
                             f"{rel} 不应自带 _cfg 实现（单一真相源 = cch_config.cfg）")
            self.assertIn("cch_config", src,
                          f"{rel} 应从 cch_config 导入 cfg")
