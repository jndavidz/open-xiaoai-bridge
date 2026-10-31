"""影子 KWS 纯逻辑测试（不加载模型）。

覆盖：现网/候选模型文件发现、缺失文件报错、影子日志 JSONL 格式。
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 直接按文件加载，避免 kws 包 __init__ 拉入 sherpa/onnxruntime 重依赖
_spec = importlib.util.spec_from_file_location(
    "kws_shadow", ROOT / "core/services/audio/kws/shadow.py"
)
_shadow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_shadow)
ShadowKws = _shadow.ShadowKws
pick_model_files = _shadow.pick_model_files


def _touch(path: Path) -> Path:
    path.write_bytes(b"")
    return path


class PickModelFilesTest(unittest.TestCase):
    def test_live_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in ("encoder.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
                _touch(d / name)
            files = pick_model_files(d)
            self.assertEqual(files["encoder"].name, "encoder.onnx")
            self.assertEqual(files["decoder"].name, "decoder.onnx")
            self.assertEqual(files["joiner"].name, "joiner.onnx")

    def test_candidate_layout_prefers_int8_and_latest_epoch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in (
                "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                "encoder-epoch-99-avg-1-chunk-16-left-64.int8.onnx",
                "decoder-epoch-99-avg-1-chunk-16-left-64.onnx",
                "joiner-epoch-99-avg-1-chunk-16-left-64.int8.onnx",
                "tokens.txt",
            ):
                _touch(d / name)
            files = pick_model_files(d)
            self.assertIn("epoch-99", files["encoder"].name)
            self.assertIn("epoch-99", files["decoder"].name)
            self.assertIn("epoch-99", files["joiner"].name)

    def test_missing_tokens_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _touch(d / "encoder.onnx")
            _touch(d / "decoder.onnx")
            _touch(d / "joiner.onnx")
            with self.assertRaises(FileNotFoundError):
                pick_model_files(d)

    def test_missing_encoder_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _touch(d / "tokens.txt")
            with self.assertRaises(FileNotFoundError):
                pick_model_files(d)


class ShadowLogTest(unittest.TestCase):
    def test_log_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            shadow = object.__new__(ShadowKws)
            shadow._log_dir = Path(tmp)
            shadow._frames = 42
            shadow._hits = 0
            shadow._log(["你好小塔"], main_hit=False)
            files = list(Path(tmp).glob("kws-shadow-*.jsonl"))
            self.assertEqual(len(files), 1)
            record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record["hits"], ["你好小塔"])
            self.assertFalse(record["main_hit"])
            self.assertEqual(record["frames"], 42)


if __name__ == "__main__":
    unittest.main()
