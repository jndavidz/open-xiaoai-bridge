"""注入闸门与标记测试（firmware-asr-injection-feasibility.md §3.4）。

覆盖：
  1. 闸门开/关/TTL 惰性失效（PC 侧崩溃后自恢复）
  2. 剩余时间观测
  3. 标记剥离（含同音词容错、正常语音不误伤）
"""

import sys
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InjectionGateTest(unittest.TestCase):
    """直接对 EventManager 闸门类方法测试（加载时桩掉重依赖）。"""

    @classmethod
    def setUpClass(cls):
        saved = {
            n: sys.modules.get(n)
            for n in [
                "core.ref", "core.services.protocols.typing",
                "core.utils.config", "core.utils.logger",
                "core.services.audio.stream", "core.services.speaker",
            ]
        }
        cls._saved = saved

        for name, mod in saved.items():
            if mod is None:
                stub = types.ModuleType(name)
                sys.modules[name] = stub
        sys.modules["core.ref"].get_app = lambda: None
        sys.modules["core.ref"].get_kws = lambda: None
        sys.modules["core.ref"].get_speaker = lambda: None
        sys.modules["core.ref"].get_xiaozhi = lambda: None
        sys.modules["core.utils.logger"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
        sys.modules["core.utils.config"].ConfigManager = types.SimpleNamespace(
            instance=lambda: None
        )
        sys.modules["core.services.protocols.typing"].AbortReason = object
        np = sys.modules.setdefault("numpy", types.ModuleType("numpy"))
        if not hasattr(np, "ndarray"):
            np.ndarray = object

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ws_test", ROOT / "core" / "wakeup_session.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.EM = module.EventManager

    @classmethod
    def tearDownClass(cls):
        for name, mod in cls._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def setUp(self):
        self.EM._injection_gate_until = 0.0

    def test_open_and_close(self):
        ttl = self.EM.open_injection_gate()
        self.assertTrue(self.EM.injection_gate_active())
        self.assertAlmostEqual(self.EM.injection_gate_remaining(), ttl, delta=1)
        self.EM.close_injection_gate()
        self.assertFalse(self.EM.injection_gate_active())
        self.assertEqual(self.EM.injection_gate_remaining(), 0)

    def test_custom_duration(self):
        self.EM.open_injection_gate(5)
        self.assertTrue(self.EM.injection_gate_active())
        self.assertLessEqual(self.EM.injection_gate_remaining(), 5)
        self.assertGreater(self.EM.injection_gate_remaining(), 4)

    def test_ttl_lazy_expiry(self):
        """PC 崩溃未关闸 → TTL 到期后惰性失效。"""
        self.EM._injection_gate_until = time.time() - 0.01
        self.assertFalse(self.EM.injection_gate_active())
        self.assertEqual(self.EM.injection_gate_remaining(), 0)

    def test_zero_duration_closes(self):
        self.EM.open_injection_gate(10)
        self.EM.open_injection_gate(0)
        self.assertFalse(self.EM.injection_gate_active())


class InjectionMarkerTest(unittest.TestCase):
    """标记剥离（机制 B）：无重依赖，直接调类方法。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        # xiaoai.py 依赖链重，全部桩掉；只需类方法 _strip_injection_marker
        stub_names = [
            "core.ref", "core.wakeup_session", "core.xiaoai_conversation",
            "core.services.speaker", "core.services.audio.stream",
            "core.services.protocols.protocol", "core.services.protocols.typing",
            "core.utils.logger", "core.utils.config", "open_xiaoai_server",
        ]
        cls._saved = {n: sys.modules.get(n) for n in stub_names}
        for name in stub_names:
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["core.ref"].get_speaker = lambda: None
        sys.modules["core.ref"].get_xiaoai = lambda: None
        sys.modules["core.ref"].set_xiaoai = lambda x: None
        sys.modules["core.ref"].get_kws = lambda: None
        sys.modules["core.ref"].get_xiaozhi = lambda: None
        sys.modules["core.ref"].get_app = lambda: None
        sys.modules["core.wakeup_session"].EventManager = types.SimpleNamespace()
        sys.modules["core.xiaoai_conversation"].XiaoAIConversationController = object
        sys.modules["core.utils.logger"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            debug=lambda *a, **k: None, error=lambda *a, **k: None,
            wakeup=lambda *a, **k: None,
        )
        sys.modules["core.utils.config"].ConfigManager = types.SimpleNamespace(
            instance=lambda: None
        )
        gs = types.ModuleType("core.services.audio.stream")
        gs.GlobalStream = type("GlobalStream", (), {})
        sys.modules["core.services.audio.stream"] = gs
        sm = types.ModuleType("core.services.speaker")
        sm.SpeakerManager = type("SpeakerManager", (), {})
        sys.modules["core.services.speaker"] = sm
        np = sys.modules.setdefault("numpy", types.ModuleType("numpy"))
        if not hasattr(np, "ndarray"):
            np.ndarray = object

        spec = importlib.util.spec_from_file_location(
            "xz_test", ROOT / "core" / "xiaoai.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.XiaoAI = module.XiaoAI

    @classmethod
    def tearDownClass(cls):
        for name, mod in cls._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_strip_cases(self):
        strip = self.XiaoAI._strip_injection_marker
        cases = {
            "注入输入今天天气怎么样": "今天天气怎么样",
            "注入输入, 帮我写个周报": "帮我写个周报",
            "注入输入。": "",
            "朱入输入测试同音": "测试同音",  # ASR 同音容错
            "今天天气怎么样": None,          # 正常语音不受影响
            "注入": None,                     # 半个标记不命中
            "": None,
        }
        for src, want in cases.items():
            self.assertEqual(strip(src), want, msg=f"{src!r}")


if __name__ == "__main__":
    unittest.main()
