# -*- coding: utf-8 -*-
"""CCH 观测影子守门（ticket 04）：只记录不控制，默认关，JSONL 留证。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "cch_observation_shadow", ROOT / "core/music/cch_observation_shadow.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class CchObservationShadowTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._mod = _mod.CchObservationShadow(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_disabled_by_default_no_log(self):
        self._mod.record_dispatch("play", "abc123", entry="kwqq:S1", epoch=1)
        self._mod.record_state("play", {"statusStr": "play"}, "22:00:00", 1.0)
        self.assertEqual([], list(Path(self._tmp.name).glob("*.jsonl")),
                         "默认关闭时不得写盘")

    def test_record_dispatch_and_state_linked_by_cmd_id(self):
        self._mod.enabled = lambda: True
        self._mod.record_dispatch("play", "abc123", entry="kwqq:S1", epoch=1)
        self._mod.record_state("play", {"statusStr": "play", "pid": 42},
                               "22:00:01", 2.0)
        files = list(Path(self._tmp.name).glob("cch-observation-*.jsonl"))
        self.assertEqual(1, len(files))
        lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
        kinds = [x["kind"] for x in lines]
        self.assertEqual(["dispatch", "state"], kinds)
        self.assertEqual("abc123", lines[0]["cmd_id"])
        self.assertEqual("kwqq:S1", lines[0]["entry"])
        self.assertEqual("play", lines[1]["extras"]["statusStr"])
        self.assertIn("ts", lines[0])

    def test_record_exception_never_breaks_caller(self):
        """影子失败不得影响主链（与 shadow KWS 同纪律）。"""
        self._mod.enabled = lambda: True
        def boom(record):
            raise OSError("simulated disk failure")
        self._mod._write = boom
        self._mod.record_dispatch("play", "x", entry=None, epoch=None)  # 不得抛
        self._mod.record_state("play", {}, "", 0.0)  # 不得抛

    def test_snapshot_counts(self):
        self._mod.enabled = lambda: True
        self._mod.record_dispatch("play", "x", entry="e", epoch=1)
        snap = self._mod.snapshot()
        self.assertGreaterEqual(snap["files"], 1)

    def test_record_position_creates_position_kind(self):
        """进度观测（只记录）：worker 已上报 progress，影子按 position 类型留证。"""
        self._mod.enabled = lambda: True
        self._mod.record_position("play", 21055, "22:10:01", 3.0)
        files = list(Path(self._tmp.name).glob("cch-observation-*.jsonl"))
        lines = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
        pos = [x for x in lines if x.get("kind") == "position"]
        self.assertEqual(1, len(pos))
        self.assertEqual(21055, pos[0]["progress_ms"])
        self.assertEqual("play", pos[0]["status"])
        self.assertEqual("position-only-no-control", pos[0]["policy"])
