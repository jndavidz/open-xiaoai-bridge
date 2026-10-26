"""对话记忆 JSONL 存储测试（spec topic-switch-and-memory 票 04）。"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services import conversation_memory as cm  # noqa: E402

SK = "agent:default:open-xiaoai-bridge"
NOW = datetime(2026, 10, 27, 12, 0, 0)


class AppendAndRotateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_append_creates_day_file_with_record(self):
        self.assertTrue(cm.append_turn(self.dir, SK, "你好", "你好呀", NOW))
        path = cm.day_path(self.dir, SK, NOW)
        self.assertTrue(path.exists())
        record = json.loads(path.read_text(encoding="utf-8").strip())
        self.assertEqual(SK, record["session_key"])
        self.assertEqual("你好", record["user"])
        self.assertEqual("你好呀", record["assistant"])
        self.assertEqual("2026-10-27T12:00:00", record["ts"])

    def test_session_key_sanitized_for_filename(self):
        cm.append_turn(self.dir, "agent:x:y?/weird", "u", "a", NOW)
        files = list(self.dir.glob("*.jsonl"))
        self.assertEqual(1, len(files))
        self.assertNotIn("/", files[0].name)

    def test_append_failure_returns_false_not_raises(self):
        # 目录路径是个文件 → mkdir 失败 → False
        blocker = self.dir / "blocker"
        blocker.write_text("x", encoding="utf-8")
        self.assertFalse(cm.append_turn(blocker, SK, "u", "a", NOW))


class PurgeExpiredTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_removes_files_older_than_retain_days(self):
        old_day = NOW - timedelta(days=40)
        cm.append_turn(self.dir, SK, "u", "a", old_day)
        cm.append_turn(self.dir, SK, "u", "a", NOW)
        removed = cm.purge_expired(self.dir, retain_days=30, now=NOW)
        self.assertEqual(1, removed)
        self.assertFalse(cm.day_path(self.dir, SK, old_day).exists())
        self.assertTrue(cm.day_path(self.dir, SK, NOW).exists())

    def test_retain_days_zero_disables_purge(self):
        cm.append_turn(self.dir, SK, "u", "a", NOW - timedelta(days=400))
        self.assertEqual(0, cm.purge_expired(self.dir, retain_days=0, now=NOW))
        self.assertEqual(1, len(list(self.dir.glob("*.jsonl"))))

    def test_non_matching_files_left_alone(self):
        stray = self.dir / "not-a-memory-file.jsonl"
        stray.write_text("x", encoding="utf-8")
        self.assertEqual(0, cm.purge_expired(self.dir, retain_days=30, now=NOW))
        self.assertTrue(stray.exists())


class RecentTurnsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_cross_day_merge_last_n(self):
        yesterday = NOW - timedelta(days=1)
        for i in range(3):
            cm.append_turn(self.dir, SK, f"u-{yesterday:%H}-{i}", f"a-{i}", yesterday)
        cm.append_turn(self.dir, SK, "u-today", "a-today", NOW)

        turns = cm.recent_turns(self.dir, SK, limit=2, now=NOW)
        self.assertEqual(2, len(turns))
        self.assertEqual("u-today", turns[-1]["user"])
        self.assertEqual("a-today", turns[-1]["assistant"])

    def test_other_session_key_files_excluded(self):
        cm.append_turn(self.dir, SK, "mine", "mine-a", NOW)
        cm.append_turn(self.dir, "agent:other:x", "theirs", "theirs-a", NOW)
        turns = cm.recent_turns(self.dir, SK, limit=5, now=NOW)
        self.assertEqual(1, len(turns))
        self.assertEqual("mine", turns[0]["user"])

    def test_corrupt_lines_skipped(self):
        path = cm.day_path(self.dir, SK, NOW)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ts": NOW.isoformat(), "session_key": SK, "user": "u1", "assistant": "a1"})
            + "\n"
            + "not-json-garbage\n"
            + "\n"
            + json.dumps({"ts": NOW.isoformat(), "session_key": SK, "user": "u2", "assistant": "a2"})
            + "\n",
            encoding="utf-8",
        )
        turns = cm.recent_turns(self.dir, SK, limit=5, now=NOW)
        self.assertEqual(2, len(turns))
        self.assertEqual(["u1", "u2"], [t["user"] for t in turns])

    def test_missing_dir_returns_empty(self):
        self.assertEqual([], cm.recent_turns(self.dir / "nope", SK, limit=5, now=NOW))


class SummaryBlockTest(unittest.TestCase):
    def test_empty_turns_empty_block(self):
        self.assertEqual("", cm.summary_block([]))

    def test_truncates_long_texts(self):
        turns = [
            {"ts": "2026-10-27T12:00:00", "session_key": SK, "user": "长" * 500, "assistant": "应" * 500}
        ]
        block = cm.summary_block(turns)
        self.assertIn("此前对话摘要", block)
        self.assertNotIn("长" * 400, block, "单轮文本超长截断")


if __name__ == "__main__":
    unittest.main()
