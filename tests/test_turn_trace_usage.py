"""TurnTrace 用量扩展（P2 ticket 03）契约测试。

覆盖：

  1. 聚合维度 note_context：首写优先；进 summary、不进日志行；
  2. 用量累计：prompt_chars 累加、llm_requests、web_calls、真实 token（忽略非法值）；
  3. 日志行仍全数值，新增数值字段在位；
  4. 双开关：trace 日志关 + 台账开 → 零日志行但落盘；台账关 → 零落盘；
  5. 台账单开（trace 关）时轮对象照常建立与收尾（喂台账路径）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.utils import turn_trace, usage_ledger  # noqa: E402

from core.utils.turn_trace import format_turn_trace  # noqa: E402


def _summary(**overrides):
    base = {
        "turn": 1,
        "src": "xiaoai_asr",
        "incomplete": 0,
        "asr_chars": 4,
        "llm_chars": -1,
        "tts_chars": -1,
        "tool_rounds": 0,
        "llm_requests": 0,
        "prompt_chars": -1,
        "web_calls": 0,
        "prompt_tokens": -1,
        "completion_tokens": -1,
        "llm_error": 0,
        "llm_timeout": 0,
        "tts_ok": -1,
        "tts_fallback": 0,
        "play_url_ok": -1,
        "playback_started": 0,
        "playback_finished": -1,
        "playback_timeout": 0,
        "speech_end_to_asr_ms": -1.0,
        "asr_to_llm_ms": -1.0,
        "llm_ms": -1.0,
        "llm_to_tts_ms": -1.0,
        "tts_ms": -1.0,
        "tts_to_play_ms": -1.0,
        "accept_to_start_ms": -1.0,
        "playback_ms": -1.0,
        "total_ms": -1.0,
    }
    base.update(overrides)
    return base


class TurnTraceUsageFieldsTest(unittest.TestCase):
    def setUp(self):
        turn_trace.reset_turn_trace(reset_counter=True)

    def tearDown(self):
        turn_trace.reset_turn_trace(reset_counter=True)

    def test_note_context_first_write_wins(self):
        trace = turn_trace.TurnTrace(1)
        trace.note_context("OpenAI", "deepseek-chat", "agent:default:x")
        trace.note_context("OpenClaw", "other", "agent:main:x")
        summary = trace.summary()
        self.assertEqual(summary["backend"], "OpenAI")
        self.assertEqual(summary["model"], "deepseek-chat")
        self.assertEqual(summary["session_key"], "agent:default:x")

    def test_usage_accumulators(self):
        trace = turn_trace.TurnTrace(1)
        trace.note_llm_request()
        trace.note_prompt_chars(100)
        trace.note_prompt_chars(50)  # 工具回环第二轮重发历史
        trace.note_web_calls(1)
        trace.note_usage_tokens(prompt=70, completion=30)
        trace.note_usage_tokens(prompt=10, completion="bad")
        summary = trace.summary()
        self.assertEqual(summary["llm_requests"], 1)
        self.assertEqual(summary["prompt_chars"], 150)
        self.assertEqual(summary["web_calls"], 1)
        self.assertEqual(summary["prompt_tokens"], 80)
        self.assertEqual(summary["completion_tokens"], 30)

    def test_usage_accumulators_ignore_invalid(self):
        trace = turn_trace.TurnTrace(1)
        trace.note_prompt_chars(-3)
        trace.note_web_calls(0)
        trace.note_usage_tokens(prompt=None, completion=True)
        summary = trace.summary()
        self.assertEqual(summary["prompt_chars"], -1)
        self.assertEqual(summary["web_calls"], 0)
        self.assertEqual(summary["prompt_tokens"], -1)
        self.assertEqual(summary["completion_tokens"], -1)

    def test_log_line_numeric_fields_and_no_context_strings(self):
        line = format_turn_trace(_summary(
            llm_requests=2, prompt_chars=320, web_calls=1,
            prompt_tokens=70, completion_tokens=30,
        ))
        self.assertIn("llm_requests=2", line)
        self.assertIn("prompt_chars=320", line)
        self.assertIn("web_calls=1", line)
        self.assertIn("prompt_tokens=70", line)
        self.assertIn("completion_tokens=30", line)
        self.assertNotIn("backend", line)
        self.assertNotIn("model=", line)
        self.assertNotIn("session_key", line)

    def test_missing_tokens_render_as_minus_one(self):
        line = format_turn_trace(_summary())
        self.assertIn("prompt_tokens=-1", line)
        self.assertIn("completion_tokens=-1", line)


class TurnTraceLedgerFeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved = {
            "trace": turn_trace.ENV_TURN_TRACE_ENABLED,
            "on": usage_ledger.ENV_USAGE_LEDGER_ENABLED,
            "dir": usage_ledger.ENV_USAGE_LEDGER_DIR,
        }
        import os

        self._os = os
        os.environ[usage_ledger.ENV_USAGE_LEDGER_DIR] = str(self.dir)

    def tearDown(self):
        import os

        for key, value in self._saved.items():
            env_key = {
                "trace": turn_trace.ENV_TURN_TRACE_ENABLED,
                "on": usage_ledger.ENV_USAGE_LEDGER_ENABLED,
                "dir": usage_ledger.ENV_USAGE_LEDGER_DIR,
            }[key]
            if value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = value
        turn_trace.reset_turn_trace(reset_counter=True)
        self._tmp.cleanup()

    def _records(self):
        files = list(self.dir.glob("usage-*.jsonl"))
        if not files:
            return []
        return [json.loads(line) for line in
                files[0].read_text(encoding="utf-8").strip().splitlines()]

    def test_ledger_only_mode_records_without_log_line(self):
        import os
        from unittest import mock

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "1"
        os.environ.pop(turn_trace.ENV_TURN_TRACE_ENABLED, None)

        with mock.patch.object(turn_trace, "logger") as mock_logger:
            trace = turn_trace.begin_turn(source="xiaoai_asr", asr_chars=4)
            self.assertIsNotNone(trace)
            trace.note_context("OpenAI", "deepseek-chat", "agent:default:x")
            turn_trace.mark_llm_start()
            turn_trace.note_llm_request()
            turn_trace.note_prompt_chars(320)
            turn_trace.note_reply_chars(96)
            turn_trace.note_tts_chars(90)
            turn_trace.mark_llm_end()
            turn_trace.mark_tts_start()
            turn_trace.mark_tts_end()
            turn_trace.note_playback(
                finished=True, started=True, reason="finished",
                wait_started_at=None, started_at_ms=None, finished_at_ms=None,
            )
        mock_logger.info.assert_not_called()

        records = self._records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["backend"], "OpenAI")
        self.assertEqual(record["model"], "deepseek-chat")
        self.assertEqual(record["session_key"], "agent:default:x")
        self.assertEqual(record["requests"], 1)
        self.assertEqual(record["prompt_chars"], 320)
        self.assertEqual(record["completion_chars"], 96)
        self.assertEqual(record["tts_chars"], 90)

    def test_ledger_off_no_file_written(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "0"
        os.environ[turn_trace.ENV_TURN_TRACE_ENABLED] = "1"
        trace = turn_trace.begin_turn(source="xiaoai_asr", asr_chars=4)
        self.assertIsNotNone(trace)
        trace.note_context("OpenAI", "m", "s")
        turn_trace.mark_llm_start()
        turn_trace.note_llm_request()
        turn_trace.mark_llm_end()
        turn_trace.abandon_turn()
        self.assertEqual(self._records(), [])

    def test_incomplete_turn_also_fed_to_ledger(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "1"
        os.environ.pop(turn_trace.ENV_TURN_TRACE_ENABLED, None)
        # 上一轮未收尾 → begin_turn 按 incomplete 收尾并喂台账
        first = turn_trace.begin_turn(source="xiaoai_asr", asr_chars=4)
        self.assertIsNotNone(first)
        second = turn_trace.begin_turn(source="xiaoai_asr", asr_chars=2)
        self.assertIsNotNone(second)
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
