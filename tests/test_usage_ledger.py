"""usage_ledger（P2 ticket 03）契约测试。

纯内存 + tmp 目录，stdlib unittest：不联网、不触 Rust 扩展。

覆盖：

  1. 开关：env 覆盖 config，默认 False；开关只 gate 记录不影响查询；
  2. observe：一条 summary = 一行 JSONL，字段映射齐全，坏 summary 不抛异常；
  3. query：按天/按模型/按后端/按会话键聚合，坏行容忍，空目录零行；
  4. token 口径：真实 usage 与字符估算分列不混加；估算 = chars / 1.6；
  5. 零控制行为：observe/query 无任何限额/切换副作用（纯落盘 + 纯读）。
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

from core.utils import usage_ledger  # noqa: E402

ENV_ON = {usage_ledger.ENV_USAGE_LEDGER_ENABLED: "1"}


def _summary(**overrides):
    base = {
        "turn": 1,
        "src": "xiaoai_asr",
        "incomplete": 0,
        "backend": "OpenAI",
        "model": "deepseek-chat",
        "session_key": "agent:default:open-xiaoai-bridge",
        "llm_requests": 2,
        "prompt_chars": 320,
        "llm_chars": 96,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tts_chars": 90,
        "tool_rounds": 1,
        "web_calls": 1,
        "llm_error": 0,
        "llm_timeout": 0,
    }
    base.update(overrides)
    return base


class UsageLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old_env = {
            k: os_environ_pop(k) for k in
            (usage_ledger.ENV_USAGE_LEDGER_ENABLED, usage_ledger.ENV_USAGE_LEDGER_DIR)
        }
        import os

        self._os = os
        os.environ[usage_ledger.ENV_USAGE_LEDGER_DIR] = str(self.dir)

    def tearDown(self):
        import os

        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # ---- 开关 ----

    def test_disabled_by_default_no_write(self):
        import os

        os.environ.pop(usage_ledger.ENV_USAGE_LEDGER_ENABLED, None)
        self.assertFalse(usage_ledger.enabled())
        self.assertFalse(usage_ledger.observe(_summary()))
        self.assertEqual(list(self.dir.glob("*.jsonl")), [])

    def test_env_override_true_forces_on(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "true"
        self.assertTrue(usage_ledger.enabled())
        self.assertTrue(usage_ledger.observe(_summary()))
        files = list(self.dir.glob("usage-*.jsonl"))
        self.assertEqual(len(files), 1)

    def test_env_override_false_beats_config(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "0"
        self.assertFalse(
            usage_ledger.enabled(config_reader=lambda: True)
        )

    def test_switch_does_not_block_query(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "0"
        (self.dir / "usage-2026-10-25.jsonl").write_text(
            json.dumps({"date": "2026-10-25", "backend": "OpenAI",
                        "model": "m", "session_key": "s", "requests": 3}) + "\n",
            encoding="utf-8",
        )
        data = usage_ledger.query(days=7, today="2026-10-26", base_dir=self.dir)
        self.assertEqual(data["totals"]["requests"], 3)

    # ---- observe 落盘 ----

    def test_observe_writes_single_jsonl_line(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "1"
        self.assertTrue(usage_ledger.observe(_summary()))
        self.assertTrue(usage_ledger.observe(_summary(turn=2)))
        path = next(self.dir.glob("usage-*.jsonl"))
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual(record["backend"], "OpenAI")
        self.assertEqual(record["model"], "deepseek-chat")
        self.assertEqual(record["session_key"], "agent:default:open-xiaoai-bridge")
        self.assertEqual(record["requests"], 2)
        self.assertEqual(record["prompt_chars"], 320)
        self.assertEqual(record["completion_chars"], 96)
        self.assertEqual(record["tts_chars"], 90)
        self.assertEqual(record["tool_rounds"], 1)
        self.assertEqual(record["web_calls"], 1)

    def test_observe_tolerates_garbage(self):
        import os

        os.environ[usage_ledger.ENV_USAGE_LEDGER_ENABLED] = "1"
        self.assertFalse(usage_ledger.observe("not-a-dict"))  # type: ignore[arg-type]
        self.assertFalse(usage_ledger.observe(None))  # type: ignore[arg-type]
        # 字段不合法的 dict 不抛异常即可（返回值随开关，无副作用承诺）
        self.assertIsInstance(usage_ledger.observe({"turn": "x"}), bool)

    # ---- query 聚合 ----

    def _seed_two_days(self):
        rows = [
            {"date": "2026-10-25", "backend": "OpenAI", "model": "deepseek-chat",
             "session_key": "agent:default:x", "requests": 2, "turns": 0,
             "prompt_chars": 160, "completion_chars": 80, "prompt_tokens": 0,
             "completion_tokens": 0, "tts_chars": 50, "tool_rounds": 1,
             "web_calls": 1, "llm_errors": 0, "llm_timeouts": 0},
            {"date": "2026-10-26", "backend": "OpenAI", "model": "deepseek-chat",
             "session_key": "agent:default:x", "requests": 1,
             "prompt_chars": 100, "completion_chars": 40, "prompt_tokens": 70,
             "completion_tokens": 30, "tts_chars": 0, "tool_rounds": 0,
             "web_calls": 0, "llm_errors": 0, "llm_timeouts": 0},
            {"date": "2026-10-26", "backend": "OpenClaw", "model": "",
             "session_key": "agent:main:x", "requests": 1,
             "prompt_chars": 0, "completion_chars": 64, "prompt_tokens": 0,
             "completion_tokens": 0, "tts_chars": 60, "tool_rounds": 0,
             "web_calls": 0, "llm_errors": 1, "llm_timeouts": 0},
        ]
        for i, row in enumerate(rows):
            day = row["date"]
            with open(self.dir / f"usage-{day}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return rows

    def test_query_aggregates_by_day_model_backend_session(self):
        self._seed_two_days()
        data = usage_ledger.query(days=7, today="2026-10-26", base_dir=self.dir)

        self.assertEqual(data["totals"]["turns"], 3)
        self.assertEqual(data["totals"]["requests"], 4)
        self.assertEqual(data["totals"]["tts_chars"], 110)
        self.assertEqual(data["totals"]["llm_errors"], 1)

        by_day = {row["date"]: row for row in data["by_day"]}
        self.assertEqual(by_day["2026-10-25"]["requests"], 2)
        self.assertEqual(by_day["2026-10-26"]["requests"], 2)
        self.assertEqual(by_day["2026-10-24"]["turns"], 0)

        by_backend = {row["backend"]: row for row in data["by_backend"]}
        self.assertEqual(by_backend["OpenAI"]["turns"], 2)
        self.assertEqual(by_backend["OpenClaw"]["llm_errors"], 1)

        by_model = {row["model"]: row for row in data["by_model"]}
        self.assertEqual(by_model["deepseek-chat"]["requests"], 3)
        self.assertEqual(by_model["-"]["requests"], 1)

        by_session = {row["session_key"]: row for row in data["by_session"]}
        self.assertEqual(by_session["agent:default:x"]["requests"], 3)
        self.assertEqual(by_session["agent:main:x"]["turns"], 1)

    def test_query_days_window_and_clamp(self):
        self._seed_two_days()
        data = usage_ledger.query(days=1, today="2026-10-26", base_dir=self.dir)
        self.assertEqual(data["totals"]["turns"], 2)
        self.assertEqual(data["days"], 1)
        # 越界夹取
        self.assertEqual(usage_ledger.query(days=999, today="2026-10-26",
                                            base_dir=self.dir)["days"], 90)
        self.assertEqual(usage_ledger.query(days=0, today="2026-10-26",
                                            base_dir=self.dir)["days"], 1)

    def test_query_tolerates_partial_and_corrupt_lines(self):
        (self.dir / "usage-2026-10-26.jsonl").write_text(
            json.dumps({"date": "2026-10-26", "backend": "OpenAI",
                        "model": "m", "session_key": "s", "requests": 1}) + "\n"
            + '{"date": "2026-10-26", "back'  # 半行（并发追加）
            + "\nnot json\n"
            + json.dumps({"date": "2026-10-26", "backend": "OpenAI",
                          "model": "m", "session_key": "s", "requests": 2}) + "\n",
            encoding="utf-8",
        )
        data = usage_ledger.query(days=1, today="2026-10-26", base_dir=self.dir)
        self.assertEqual(data["totals"]["requests"], 3)

    def test_query_empty_dir_zero_rows(self):
        data = usage_ledger.query(days=3, today="2026-10-26", base_dir=self.dir)
        self.assertEqual(data["totals"]["turns"], 0)
        self.assertEqual(data["by_model"], [])
        self.assertEqual(len(data["by_day"]), 3)

    # ---- token 口径 ----

    def test_estimation_columns_split_from_real_tokens(self):
        self._seed_two_days()
        data = usage_ledger.query(days=7, today="2026-10-26", base_dir=self.dir)
        totals = data["totals"]
        # 估算列 = 全量字符 / 1.6：prompt (160+100)/1.6、completion (80+40+64)/1.6
        self.assertEqual(totals["prompt_tokens_est"], int(260 / 1.6))
        self.assertEqual(totals["completion_tokens_est"], int(184 / 1.6))
        # 真实列只含报了 usage 的请求（10-26 OpenAI 那轮）
        self.assertEqual(totals["prompt_tokens"], 70)
        self.assertEqual(totals["completion_tokens"], 30)

    def test_estimate_tokens(self):
        self.assertEqual(usage_ledger.estimate_tokens(0), 0)
        self.assertEqual(usage_ledger.estimate_tokens(-5), 0)
        self.assertEqual(usage_ledger.estimate_tokens(160), 100)

    def test_estimation_note_present(self):
        data = usage_ledger.query(days=1, today="2026-10-26", base_dir=self.dir)
        self.assertIn("1.6", data["estimation_note"])
        self.assertIn("无任何限额", data["estimation_note"])


def os_environ_pop(key: str):
    import os

    return os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
