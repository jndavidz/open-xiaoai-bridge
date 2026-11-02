"""逐词门槛（:boost #threshold）测试（music-far spec 票 03）。

只测外部行为：词表编译产物每行携带什么后缀、显示名是否仍是原词、自检是否
报出坏行。语法已对照 sherpa-onnx 官方文档核实（2026-11-03）：
``<tokens> [:<boost>] [#<threshold>] [@<显示名>]``。不加载 sherpa 真模型。
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
    "kws_word_gates", ROOT / "core/utils/word_gates.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

compile_keyword_line = _mod.compile_keyword_line
parse_keyword_line = _mod.parse_keyword_line
verify_keyword_lines = _mod.verify_keyword_lines


def _load_keywords_module():
    """按文件加载 keywords.py（get_word_gates 用；依赖 core.utils 链可正常导入）。"""
    spec = importlib.util.spec_from_file_location(
        "kws_keywords_gates", ROOT / "core/services/audio/kws/keywords.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompileLineTest(unittest.TestCase):
    def test_no_gate_is_byte_identical_to_legacy(self):
        """无门槛词与今天逐字节一致（全局默认兜底）。"""
        self.assertEqual(compile_keyword_line(["n", "ǐ"], "你好"), "n ǐ @你好")

    def test_boost_suffix(self):
        line = compile_keyword_line(["t", "ō"], "停止聆听", {"boost": 2.5})
        self.assertEqual(line, "t ō :2.5 @停止聆听")

    def test_threshold_suffix(self):
        line = compile_keyword_line(["t", "ō"], "停止聆听", {"threshold": 0.4})
        self.assertEqual(line, "t ō #0.4 @停止聆听")

    def test_both_suffixes_display_name_always_last(self):
        line = compile_keyword_line(
            ["x", "i", "ǎ"], "下一首歌曲", {"boost": 1.2, "threshold": 0.1}
        )
        self.assertEqual(line, "x i ǎ :1.2 #0.1 @下一首歌曲")
        self.assertTrue(line.rstrip().split()[-1].startswith("@"))

    def test_empty_gate_is_ignored(self):
        self.assertEqual(compile_keyword_line(["a"], "词", {}), "a @词")


class ParseLineTest(unittest.TestCase):
    def test_round_trip(self):
        parsed = parse_keyword_line("n ǐ :2.5 #0.4 @你好")
        self.assertEqual(parsed["tokens"], ["n", "ǐ"])
        self.assertEqual(parsed["boost"], 2.5)
        self.assertEqual(parsed["threshold"], 0.4)
        self.assertEqual(parsed["display"], "你好")
        self.assertEqual(parsed["malformed"], [])

    def test_malformed_suffix_reported(self):
        parsed = parse_keyword_line("n ǐ :abc @你好")
        self.assertEqual(parsed["malformed"], [":abc"])
        self.assertIsNone(parsed["boost"])

    def test_missing_display_name(self):
        self.assertIsNone(parse_keyword_line("n ǐ")["display"])


class VerifyLinesTest(unittest.TestCase):
    def test_complete_file_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keywords.txt"
            path.write_text(
                "n ǐ :2.5 #0.4 @停止聆听\nt ō @下一首歌曲\n", encoding="utf8"
            )
            self.assertEqual(
                verify_keyword_lines(path, ["停止聆听", "下一首歌曲"]), []
            )

    def test_lost_display_name_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keywords.txt"
            path.write_text("n ǐ :2.5 @\n", encoding="utf8")
            problems = verify_keyword_lines(path, ["停止聆听"])
            self.assertTrue(
                any("显示名" in problem for problem in problems), problems
            )

    def test_corrupted_line_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keywords.txt"
            path.write_text("n ǐ :abc @停止聆听\n", encoding="utf8")
            problems = verify_keyword_lines(path, ["停止聆听"])
            self.assertTrue(any(":abc" in problem for problem in problems), problems)

    def test_line_count_mismatch_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keywords.txt"
            path.write_text("n ǐ @停止聆听\n", encoding="utf8")
            problems = verify_keyword_lines(path, ["停止聆听", "你好小塔"])
            self.assertTrue(any("行数" in problem for problem in problems), problems)


class GetWordGatesTest(unittest.TestCase):
    class _FakeConfig:
        def __init__(self, gates):
            self._gates = gates

        def get_app_config(self, path, default=None):
            if path == "kws.keyword_gates":
                return self._gates
            return default

    def _run(self, gates):
        mod = _load_keywords_module()
        with mock.patch.object(
            mod.ConfigManager, "instance", return_value=self._FakeConfig(gates)
        ):
            return mod.get_word_gates()

    def test_valid_gates_pass_through(self):
        gates = self._run({"停止聆听": {"boost": 2.5, "threshold": 0.4}})
        self.assertEqual(gates, {"停止聆听": {"boost": 2.5, "threshold": 0.4}})

    def test_non_numeric_value_dropped(self):
        gates = self._run({"停止聆听": {"boost": "loud"}})
        self.assertEqual(gates, {})
        gates = self._run({"停止聆听": {"boost": None, "threshold": 0.4}})
        self.assertEqual(gates, {"停止聆听": {"threshold": 0.4}})

    def test_non_dict_config_ignored(self):
        self.assertEqual(self._run(["停止聆听"]), {})


class MainWritesGatesTest(unittest.TestCase):
    """main() 产物：带门槛词的后缀进文件、显示名仍是原词、坏行触发告警。"""

    def _run_main(self, tmp: Path, gates: dict):
        mod = _load_keywords_module()
        (tmp / "tokens.txt").write_text("", encoding="utf-8")
        fake_encoded = [["n", "ǐ"], ["t", "ō"]]

        class _FakeConfig:
            def get_app_config(self, path, default=None):
                if path == "kws.keyword_gates":
                    return gates
                if path == "wakeup.keywords":
                    return ["停止聆听", "下一首歌曲"]
                return default

        with mock.patch.object(mod, "get_model_file_path",
                               side_effect=lambda name: str(tmp / name)), \
                mock.patch.object(mod.ConfigManager, "instance",
                                  return_value=_FakeConfig()), \
                mock.patch.dict(sys.modules, {"sherpa_onnx": mock.MagicMock()}), \
                mock.patch.dict("os.environ", {"OPENAI_ENABLE": "1"}):
            import sherpa_onnx  # noqa: F401

            sys.modules["sherpa_onnx"].text2token.return_value = fake_encoded
            rc = mod.main()
        return rc

    def test_gated_lines_written_and_self_check_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run_main(Path(tmp), {"停止聆听": {"threshold": 0.4}})
            self.assertEqual(rc, 0)
            lines = (Path(tmp) / "keywords.txt").read_text(encoding="utf8").splitlines()
            self.assertEqual(lines[0], "n ǐ #0.4 @停止聆听")
            self.assertEqual(lines[1], "t ō @下一首歌曲")

    def test_no_gates_config_keeps_legacy_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self._run_main(Path(tmp), {})
            self.assertEqual(rc, 0)
            lines = (Path(tmp) / "keywords.txt").read_text(encoding="utf8").splitlines()
            self.assertEqual(lines[0], "n ǐ @停止聆听")
            self.assertEqual(lines[1], "t ō @下一首歌曲")


class ShadowBuildKeywordsGatesTest(unittest.TestCase):
    """影子臂编译走同一 helper：gates 进后缀、显示名不变。"""

    def test_shadow_keywords_with_gates(self):
        spec = importlib.util.spec_from_file_location(
            "kws_shadow_gates", ROOT / "core/services/audio/kws/shadow.py"
        )
        shadow_mod = importlib.util.module_from_spec(spec)
        # ⚠ 保存/恢复（勿直接 pop）：套件里可能有先于此运行的测试留下**永久**
        # sherpa stub（asr.service 等后续 import 依赖它缓存），pop 会拆掉别人的桥。
        previous = sys.modules.get("sherpa_onnx")
        sys.modules["sherpa_onnx"] = mock.MagicMock()
        try:
            spec.loader.exec_module(shadow_mod)
            sys.modules["sherpa_onnx"].text2token.return_value = [["t", "ō"]]
            with tempfile.TemporaryDirectory() as tmp:
                model_dir = Path(tmp) / "model"
                model_dir.mkdir()
                (model_dir / "tokens.txt").write_text("", encoding="utf-8")
                target = Path(tmp) / "out" / "shadow-keywords.txt"
                shadow_mod.build_keywords(
                    ["停止聆听"], model_dir, target, gates={"停止聆听": {"boost": 2.5}}
                )
                self.assertEqual(
                    target.read_text(encoding="utf8").strip(),
                    "t ō :2.5 @停止聆听",
                )
        finally:
            if previous is None:
                sys.modules.pop("sherpa_onnx", None)
            else:
                sys.modules["sherpa_onnx"] = previous


if __name__ == "__main__":
    unittest.main()
