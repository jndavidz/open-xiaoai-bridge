"""keywords.py 词表编译的模型适配测试（stdlib unittest，不加载 sherpa）。

覆盖：tokens_type 决策随模型目录形态自适应——
  - 有 bpe.model（现网 cjkchar+bpe 模型）：cjkchar+bpe，bpe.model 必需；
  - 无 bpe.model（候选 wenetspeech 拼音 tokens 模型）：ppinyin，bpe.model 不再必需
    （2026-11-01 主路切候选后启动崩溃循环的根因：keywords.py 硬编码 cjkchar+bpe）。
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "kws_keywords", ROOT / "core/services/audio/kws/keywords.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class _FakeConfig:
    def get_app_config(self, _path, default=None):
        return ["你好小塔"]


class TokensTypeDecisionTest(unittest.TestCase):
    def _run_get_args(self, tmp: Path, with_bpe: bool):
        tokens = tmp / "tokens.txt"
        tokens.write_text("", encoding="utf-8")
        if with_bpe:
            (tmp / "bpe.model").write_bytes(b"x")
        with mock.patch.object(_mod, "get_model_file_path", side_effect=lambda name: str(tmp / name)), \
                mock.patch.object(_mod.ConfigManager, "instance", return_value=_FakeConfig()):
            return _mod.get_args()

    def test_with_bpe_model_uses_cjkchar_bpe(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._run_get_args(Path(tmp), with_bpe=True)
            self.assertEqual(args["tokens_type"], "cjkchar+bpe")
            self.assertTrue(args["has_bpe"])

    def test_without_bpe_model_uses_ppinyin(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._run_get_args(Path(tmp), with_bpe=False)
            self.assertEqual(args["tokens_type"], "ppinyin")
            self.assertFalse(args["has_bpe"])

    def test_missing_tokens_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bpe.model").write_bytes(b"x")
            with mock.patch.object(_mod, "get_model_file_path", side_effect=lambda name: str(tmp / name)), \
                    mock.patch.object(_mod.ConfigManager, "instance", return_value=_FakeConfig()):
                missing = _mod.missing_required_files()
            self.assertIn(str(tmp / "tokens.txt"), missing)
            self.assertNotIn(str(tmp / "bpe.model"), missing)


class DisplayNameTest(unittest.TestCase):
    """main() 写出的 keywords_file 显示名必须是原词（@串进唤醒文本参与路由）。"""

    def test_main_writes_original_word_display_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "tokens.txt").write_text("", encoding="utf-8")
            fake_encoded = [["n", "ǐ", "h", "ǎo", "x", "iǎo", "t", "ǎ"]]
            with mock.patch.object(_mod, "get_model_file_path", side_effect=lambda name: str(tmp / name)), \
                    mock.patch.object(_mod.ConfigManager, "instance", return_value=_FakeConfig()), \
                    mock.patch.dict(sys.modules, {"sherpa_onnx": mock.MagicMock()}), \
                    mock.patch.dict("os.environ", {"OPENAI_ENABLE": "1"}):
                import sherpa_onnx  # noqa: F401 - 保证 patch 后 import 命中 stub
                sys.modules["sherpa_onnx"].text2token.return_value = fake_encoded
                rc = _mod.main()
            self.assertEqual(rc, 0)
            line = (tmp / "keywords.txt").read_text(encoding="utf-8").strip()
            self.assertEqual(line, "n ǐ h ǎo x iǎo t ǎ @你好小塔")


if __name__ == "__main__":
    unittest.main()
