"""评测结果口径契约测试（music-far spec 票 01）。

被测对象 = deploy/lx06/kws-beamform-ab/eval_contract.py（纯 stdlib，按文件加载，
避免 harness 目录进 bridge import path）。spec Testing Decisions 要求校验函数
至少 3 个用例：完整结果通过 / 缺 split 报错 / in-sample 与 held-out 混入同一
文件报错——另补字段类型、枚举值、JSONL、负样本小时数与 CLI 退出码覆盖。
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BRIDGE_ROOT.parent
CONTRACT = REPO_ROOT / "deploy/lx06/kws-beamform-ab/eval_contract.py"

_spec = importlib.util.spec_from_file_location("eval_contract", CONTRACT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

validate_result = _mod.validate_result
validate_result_file = _mod.validate_result_file


def _full_record(**overrides) -> dict:
    record = {
        "arm": "wenetspeech-3.3M",
        "channel": "strongest",
        "vad_mode": "gate-0.2",
        "score": 1.0,
        "threshold": 0.25,
        "n_pos": 50,
        "n_neg": 400,
        "n_neg_hours": 6.5,
        "split": "held-out-speaker",
        "per_word_hits": {"你好小塔": {"pos": 25, "hits": 22}},
    }
    record.update(overrides)
    return record


class ValidateResultTest(unittest.TestCase):
    def test_complete_result_passes(self):
        """用例 1：完整结果通过。"""
        self.assertEqual(validate_result(_full_record()), [])

    def test_missing_split_reports_readable_error(self):
        """用例 2：缺 split 报错（点名字段）。"""
        record = _full_record()
        del record["split"]
        errors = validate_result(record)
        self.assertEqual(len(errors), 1)
        self.assertIn("split", errors[0])

    def test_every_required_field_is_enforced(self):
        for field in _mod.REQUIRED_FIELDS:
            record = _full_record()
            del record[field]
            errors = validate_result(record)
            self.assertTrue(
                any(field in message for message in errors),
                f"缺 {field} 应被点名，实际：{errors}",
            )

    def test_non_dict_record_rejected(self):
        self.assertTrue(validate_result([1, 2, 3]))

    def test_invalid_split_value_rejected(self):
        errors = validate_result(_full_record(split="holdout"))
        self.assertTrue(any("split" in message for message in errors))

    def test_invalid_channel_and_vad_mode_rejected(self):
        errors = validate_result(_full_record(channel="ch9", vad_mode="always-on"))
        self.assertTrue(any("channel" in message for message in errors))
        self.assertTrue(any("vad_mode" in message for message in errors))

    def test_endpoint_vad_mode_accepted(self):
        """票 02 起生产主路 = endpoint 语义，评测必须能合法标注该口径。"""
        self.assertEqual(validate_result(_full_record(vad_mode="endpoint")), [])

    def test_wrong_types_rejected(self):
        errors = validate_result(
            _full_record(n_pos="50", score="1.0", per_word_hits=[])
        )
        self.assertEqual(len(errors), 3)

    def test_negative_counts_rejected(self):
        errors = validate_result(_full_record(n_pos=-1, n_neg_hours=-0.5))
        self.assertTrue(any("n_pos" in message for message in errors))
        self.assertTrue(any("n_neg_hours" in message for message in errors))

    def test_bool_is_not_a_number(self):
        errors = validate_result(_full_record(score=True))
        self.assertTrue(any("score" in message for message in errors))


class ValidateResultFileTest(unittest.TestCase):
    def test_insample_and_heldout_mixed_in_one_file_rejected(self):
        """用例 3：in-sample 与 held-out 混入同一文件报错。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.json"
            path.write_text(
                json.dumps(
                    [
                        _full_record(split="in-sample"),
                        _full_record(split="held-out-speaker"),
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            errors = validate_result_file(path)
        self.assertTrue(any("混入" in message for message in errors), errors)

    def test_uniform_insample_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "insample.json"
            path.write_text(
                json.dumps([_full_record(split="in-sample") for _ in range(2)]),
                encoding="utf-8",
            )
            self.assertEqual(validate_result_file(path), [])

    def test_jsonl_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            lines = [
                json.dumps(_full_record(), ensure_ascii=False),
                json.dumps(
                    _full_record(n_neg_hours=None), ensure_ascii=False
                ),
            ]
            path.write_text("\n".join(lines), encoding="utf-8")
            errors = validate_result_file(path)
        self.assertTrue(any("n_neg_hours" in message for message in errors))

    def test_missing_file_and_empty_file_reported(self):
        errors = validate_result_file("Z:/definitely/not/here.json")
        self.assertTrue(errors)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.json"
            path.write_text("[]", encoding="utf-8")
            self.assertTrue(validate_result_file(path))


class CliTest(unittest.TestCase):
    def _run(self, *paths):
        import os

        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-I", str(CONTRACT), *paths],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    def test_valid_file_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.json"
            path.write_text(json.dumps(_full_record()), encoding="utf-8")
            result = self._run(str(path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_file_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            record = _full_record()
            del record["channel"]
            path.write_text(json.dumps(record), encoding="utf-8")
            result = self._run(str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("channel", result.stdout)


if __name__ == "__main__":
    unittest.main()
