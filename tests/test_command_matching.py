"""指令归一化与别名键解析测试（B-31，评审 W4 无行为变化底座）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.commands.matching import (  # noqa: E402
    normalize_command_text,
    resolve_command_key,
)

TABLE = {"打开Kodi": ["步骤"], "音乐模式": ["步骤"], "停止音乐": ["步骤"]}


class NormalizeTest(unittest.TestCase):
    def test_latin_case_insensitive(self):
        self.assertEqual(normalize_command_text("打开Kodi"), normalize_command_text("打开kodi"))
        self.assertEqual(normalize_command_text("打开KODI"), normalize_command_text("打开Kodi"))

    def test_strips_punctuation_and_whitespace(self):
        self.assertEqual(normalize_command_text("打开 Kodi。"), normalize_command_text("打开Kodi"))

    def test_fullwidth_to_halfwidth(self):
        self.assertEqual(
            normalize_command_text("打开Ｋｏｄｉ"), normalize_command_text("打开kodi")
        )

    def test_empty_safe(self):
        self.assertEqual("", normalize_command_text(""))
        self.assertEqual("", normalize_command_text("，。！"))


class ResolveKeyTest(unittest.TestCase):
    def test_exact_hit_unchanged_semantics(self):
        self.assertEqual("打开Kodi", resolve_command_key("打开Kodi", TABLE))

    def test_case_variation_hits_via_normalization(self):
        self.assertEqual("打开Kodi", resolve_command_key("打开KODI", TABLE))

    def test_punctuated_asr_hits_via_normalization(self):
        self.assertEqual("音乐模式", resolve_command_key("音乐，模式", TABLE))

    def test_miss_stays_miss(self):
        """不同词不因归一化而误命中（归一化不是模糊匹配）。"""
        self.assertIsNone(resolve_command_key("打开电脑", TABLE))
        self.assertIsNone(resolve_command_key("哈哈模式", TABLE))

    def test_alias_maps_to_canonical_key(self):
        aliases = {"打开CODY": "打开Kodi", "阿尔法模式": "HiFi模式"}
        table = dict(TABLE, **{"HiFi模式": ["步骤"]})
        self.assertEqual("打开Kodi", resolve_command_key("打开CODY", table, aliases))
        self.assertEqual("HiFi模式", resolve_command_key("阿尔法模式", table, aliases))

    def test_alias_with_punctuation_noise(self):
        aliases = {"打开CODY": "打开Kodi"}
        self.assertEqual("打开Kodi", resolve_command_key("打开，CODY", TABLE, aliases))

    def test_dangling_alias_ignored_not_fatal(self):
        aliases = {"幽灵别名": "不存在的键"}
        self.assertIsNone(resolve_command_key("幽灵别名", TABLE, aliases))

    def test_no_aliases_identical_to_plain_lookup(self):
        """别名表缺省 = 现状逐字等价（无行为变化承诺的守门断言）。"""
        self.assertEqual("音乐模式", resolve_command_key("音乐模式", TABLE, None))
        self.assertIsNone(resolve_command_key("音乐模式吧", TABLE, None))

    def test_empty_text_or_table(self):
        self.assertIsNone(resolve_command_key("", TABLE))
        self.assertIsNone(resolve_command_key("音乐模式", {}, None))


if __name__ == "__main__":
    unittest.main()
