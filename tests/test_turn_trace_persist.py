"""TurnTrace 落盘面测试（B-32/A4：summary JSONL 持久化）。"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils import turn_trace  # noqa: E402


class TurnTracePersistTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved_dir = turn_trace._PERSIST_DIR
        turn_trace._PERSIST_DIR = self.dir
        self.addCleanup(setattr, turn_trace, "_PERSIST_DIR", self._saved_dir)
        self.addCleanup(turn_trace.reset_turn_trace)

    def _summary(self):
        return {
            "turn": 1,
            "total_ms": 1234.5,
            "llm_first_token_ms": 300.0,
            "incomplete": 0,
            "backend": "OpenAI",
            "model": "",
            "session_key": "agent:default:x",
        }

    def test_persist_enabled_env_parsing(self):
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_PERSIST: "1"}):
            self.assertTrue(turn_trace.persist_enabled())
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_PERSIST: "0"}):
            self.assertFalse(turn_trace.persist_enabled())
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_PERSIST: ""}):
            with mock.patch.object(turn_trace, "_read_config_persist", return_value=True):
                self.assertTrue(turn_trace.persist_enabled(), "env 空回落 config")
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_PERSIST: ""}):
            with mock.patch.object(turn_trace, "_read_config_persist", return_value=False):
                self.assertFalse(turn_trace.persist_enabled())

    def test_persist_writes_one_line_with_ts(self):
        turn_trace._persist_summary(self._summary())
        files = list(self.dir.glob("turntrace-*.jsonl"))
        self.assertEqual(1, len(files))
        record = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(1, record["turn"])
        self.assertIn("ts", record)
        self.assertIn("llm_first_token_ms", record)
        self.assertNotIn("user_text", record, "无 PII：原文不落盘")

    def test_emit_persists_when_enabled(self):
        with mock.patch.object(turn_trace, "persist_enabled", return_value=True), \
             mock.patch.object(turn_trace, "trace_enabled", return_value=False), \
             mock.patch.object(turn_trace.usage_ledger, "enabled", return_value=False):
            trace = turn_trace.begin_turn("local_asr", asr_chars=2, turn_id=1)
            self.assertIsNotNone(trace)
            turn_trace.mark_llm_end()
            # 直接收尾触发 _emit → persist
            turn_trace.abandon_turn()

        files = list(self.dir.glob("turntrace-*.jsonl"))
        self.assertEqual(1, len(files), "abandon 路径经 _emit 落盘")
        record = json.loads(files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(1, record["turn"])
        self.assertEqual(1, record["incomplete"])

    def test_concurrent_appends_serialize(self):
        import threading

        with mock.patch.object(turn_trace, "persist_enabled", return_value=True):
            threads = [
                threading.Thread(target=turn_trace._persist_summary, args=(self._summary(),))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        files = list(self.dir.glob("turntrace-*.jsonl"))
        self.assertEqual(1, len(files))
        lines = [ln for ln in files[0].read_text(encoding="utf-8").splitlines() if ln]
        self.assertEqual(8, len(lines), "并发写全量落盘（锁串行）")


if __name__ == "__main__":
    unittest.main()
