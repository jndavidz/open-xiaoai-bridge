"""主路 KeywordSpotter 的启动期参数来源测试（stdlib unittest，不加载真实模型）。

背景（2026-11-02 配置回写）：生产已于 2026-11-01 切候选模型（score 1.0 /
threshold 0.25 / VAD 0.2），而仓库 deploy/config.py 仍停在 2.0/0.30 + VAD 0.4；
同时主路 beam 宽度硬编码 8，配置层只有影子有 `max_active_paths`——主路与影子
调参不对称，线上实际工作点只能靠读源码知道。

本测试锁定外部行为：主路 `KeywordSpotter` 的 keywords_score / keywords_threshold /
max_active_paths 三个启动期参数**全部来自 kws.* 配置**，缺省回落到已知安全值。
不加载真实模型、不触碰音频硬件：sherpa_onnx 用桩替身。
"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _StubSpotter:
    """KeywordSpotter 桩：只记录构造 kwargs。"""

    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = dict(kwargs)

    def create_stream(self):
        return object()


class _FakeConfig:
    def __init__(self, kws: dict | None = None):
        self._kws = dict(kws or {})

    def get_app_config(self, path, default=None):
        if path.startswith("kws."):
            return self._kws.get(path.split(".", 1)[1], default)
        return default


_MOD = None


def _load_sherpa_module():
    """按文件加载 sherpa.py（仅一次），sherpa_onnx 用桩替身。"""
    global _MOD
    if _MOD is not None:
        return _MOD
    stub = types.ModuleType("sherpa_onnx")
    stub.KeywordSpotter = _StubSpotter
    spec = importlib.util.spec_from_file_location(
        "kws_sherpa_cfg", ROOT / "core/services/audio/kws/sherpa.py"
    )
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("sherpa_onnx")
    sys.modules["sherpa_onnx"] = stub
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("sherpa_onnx", None)
        else:
            sys.modules["sherpa_onnx"] = previous
    _MOD = module
    return module


class MainKwsParamsFromConfigTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_sherpa_module()
        _StubSpotter.last_kwargs = None

    def _start(self, kws_config: dict) -> dict:
        with mock.patch.object(
            self.mod.ConfigManager, "instance", return_value=_FakeConfig(kws_config)
        ), mock.patch.object(
            self.mod, "get_model_file_path", side_effect=lambda name: f"/models/{name}"
        ):
            self.mod.SherpaOnnx.start()
        return _StubSpotter.last_kwargs

    def test_beam_width_comes_from_config(self):
        kwargs = self._start({"max_active_paths": 16})
        self.assertEqual(kwargs["max_active_paths"], 16)

    def test_beam_width_defaults_to_8(self):
        kwargs = self._start({})
        self.assertEqual(kwargs["max_active_paths"], 8)

    def test_beam_width_is_clamped_to_minimum_one(self):
        kwargs = self._start({"max_active_paths": 0})
        self.assertEqual(kwargs["max_active_paths"], 1)

    def test_score_and_threshold_come_from_config(self):
        kwargs = self._start({"keywords_score": 1.0, "keywords_threshold": 0.25})
        self.assertEqual(kwargs["keywords_score"], 1.0)
        self.assertEqual(kwargs["keywords_threshold"], 0.25)

    def test_single_thread_stays_pinned(self):
        """单线程是 N3060 上的硬约束（RTF 预算），不得被配置化稀释。"""
        kwargs = self._start({"max_active_paths": 8})
        self.assertEqual(kwargs["num_threads"], 1)


if __name__ == "__main__":
    unittest.main()