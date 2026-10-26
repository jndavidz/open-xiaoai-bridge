"""音乐生态点歌触发词守门（V2 / music-ecosystem-voice 票 02）。

纯数据面断言（不连设备、不建 KeywordSpotter）：
  1. 根 config 与 deploy config 的 wakeup.keywords 均含点歌触发词（误删即红）；
  2. 两张路由表（WAKEWORD_ROUTES / PERSONAS）均有对应条目——KWS 命中后
     router 的 kws 分支表/人设未命中会 return None 不进对话，词表加了词
     而路由表缺条目 = 「喊了没反应」，两个面必须成对；
  3. 触发词与词表既有词互不为子串（词表内嵌套会造成模糊命中歧义，
     参照 incident-kws-self-trigger-loop 的词表×文案教训）；
  4. 路由条目 intro 不含任何 KWS 词表词（announce 词表检查默认拒绝，
     intro 命中词表 = 开场白永远播不出去且留 WARN）。

数据源 = 真实 config 文件（同 test_kws_audit 的 deploy 守门口径）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())


def _load_config(relpath: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRIGGER_WORDS = ["我要点歌", "帮我点歌"]


class MusicTriggerWordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_cfg = _load_config("config.py", "root_config_for_music_triggers")
        cls.deploy_cfg = _load_config(
            "deploy/config.py", "deploy_config_for_music_triggers"
        )

    def _keywords(self, cfg):
        return list(cfg.APP_CONFIG["wakeup"]["keywords"])

    def test_trigger_words_in_root_keywords(self):
        missing = [w for w in TRIGGER_WORDS if w not in self._keywords(self.root_cfg)]
        self.assertEqual(missing, [])

    def test_trigger_words_in_deploy_keywords(self):
        missing = [w for w in TRIGGER_WORDS if w not in self._keywords(self.deploy_cfg)]
        self.assertEqual(missing, [])

    def test_trigger_words_have_route_entries(self):
        for table_name in ("WAKEWORD_ROUTES",):
            table = getattr(self.root_cfg, table_name)
            missing = [w for w in TRIGGER_WORDS if w not in table]
            self.assertEqual(missing, [], f"{table_name} 缺条目 {missing}")
        for word in TRIGGER_WORDS:
            entry = self.deploy_cfg.PERSONAS[word]
            self.assertEqual(entry["backend"], "openai")

    def test_trigger_words_not_substring_of_existing(self):
        for cfg in (self.root_cfg, self.deploy_cfg):
            words = self._keywords(cfg)
            for trigger in TRIGGER_WORDS:
                others = [w for w in words if w != trigger]
                embedded = [w for w in others if trigger in w or w in trigger]
                self.assertEqual(embedded, [], f"{trigger} 与词表词嵌套: {embedded}")
        # 指令表维度（deploy 双表，KWS 词表编译口径的一部分）：触发词与
        # 既有指令键互不为子串，否则免唤醒路由先吞、命中歧义
        for table_name in ("DIRECT_COMMANDS", "XIAOAI_COMMANDS"):
            table = getattr(self.deploy_cfg, table_name, None)
            if not isinstance(table, dict):
                continue
            for trigger in TRIGGER_WORDS:
                hits = [k for k in table if trigger in k or k in trigger]
                self.assertEqual(hits, [], f"{trigger} 与 {table_name} 键嵌套: {hits}")

    def test_route_intro_free_of_kws_words(self):
        intros = [
            self.root_cfg.WAKEWORD_ROUTES[w]["intro"] for w in TRIGGER_WORDS
        ] + [self.deploy_cfg.PERSONAS[w]["intro"] for w in TRIGGER_WORDS]
        for cfg in (self.root_cfg, self.deploy_cfg):
            words = self._keywords(cfg)
            for intro in intros:
                hits = [w for w in words if w in intro]
                self.assertEqual(hits, [], f"intro「{intro}」命中词表词 {hits}")


if __name__ == "__main__":
    unittest.main()
