"""KWS 播放闸门测试（incident-kws-self-trigger-loop.md §5 建议 1）。

不依赖 sherpa_onnx / 音频硬件：直接对闸门状态机与 SherpaOnnx.reset 打桩。
覆盖：
  1. 引用计数（并发播报不互相踩踏）
  2. 会话暂停与闸门正交（派生 paused）
  3. 兜底自动恢复（调用方遗忘 gate_off 时不永久失效）
  4. 恢复时 reset Sherpa 流（AGENTS.md:151-158 回声泄漏教训）
  5. 原有 pause/resume/listen_disabled 语义不回归
"""

import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _load_kws_module():
    """在桩掉重型依赖的前提下加载 KWS 模块。

    core.services.audio.kws.__init__ 在导入期即构造单例 `KWS`，会拉起
    sherpa/audio 依赖；这里用 sys.modules 桩把无关依赖替换掉。

    ⚠️ 注意：本函数会**临时覆盖 sys.modules**，调用方必须保存/恢复，
    否则会污染同进程内其它测试（如 test_wakeup_keywords）。
    返回 (module, saved_modules)。
    """
    _STUB_NAMES = [
        "core.services.audio.kws.sherpa", "core.ref",
        "core.services.protocols.typing", "core.utils.config",
        "core.utils.logger", "core.services.audio.stream",
        "core.services.audio.vad.silero", "core.wakeup_session",
    ]
    saved = {n: sys.modules.get(n) for n in _STUB_NAMES}
    # 桩：sherpa 后端
    sherpa_mod = types.ModuleType("core.services.audio.kws.sherpa")

    class _FakeSherpa:
        def __init__(self):
            self.reset_calls = 0

        def start(self):
            pass

        def reset(self):
            self.reset_calls += 1

        def kws(self, frames):
            return None

    sherpa_mod.SherpaOnnx = _FakeSherpa()
    # 桩：其余被 __init__ 顶层 import 的模块
    for name in ("core.ref", "core.services.protocols.typing", "core.utils.config"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    # core.ref: 提供 set_kws / get_app / get_xiaoai / get_xiaozhi
    ref = sys.modules["core.ref"]
    ref.set_kws = lambda k: None
    ref.get_app = lambda: None
    ref.get_xiaoai = lambda: types.SimpleNamespace(async_loop=None)
    ref.get_xiaozhi = lambda: None
    ref.get_kws = lambda: None
    # typing: AudioConfig / DeviceState
    typing = sys.modules["core.services.protocols.typing"]
    typing.AudioConfig = types.SimpleNamespace(FORMAT="int16", FRAME_SIZE=512)
    typing.DeviceState = types.SimpleNamespace(LISTENING="listening", SPEAKING="speaking")
    # config
    cfg = sys.modules["core.utils.config"]

    class _FakeConfigManager:
        @staticmethod
        def instance():
            return _FakeConfigManager()

        def get_app_config(self, path, default=None):
            return default

        def add_reload_listener(self, *_a):
            pass

    cfg.ConfigManager = _FakeConfigManager
    # logger
    logmod = types.ModuleType("core.utils.logger")

    class _L:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    logmod.logger = _L()
    sys.modules.setdefault("core.utils.logger", logmod)
    # 队列/流/会话
    for name in (
        "core.services.audio.stream",
        "core.services.audio.vad.silero",
        "core.wakeup_session",
    ):
        m = types.ModuleType(name)
        if name.endswith("stream"):
            m.MyAudio = types.SimpleNamespace(create=lambda: None)
        if name.endswith("silero"):
            m.Silero = types.SimpleNamespace(vad=lambda *a, **k: 0.0)
        if name.endswith("wakeup_session"):
            class _EM:
                @staticmethod
                async def wakeup(*a, **k):
                    return None

            m.EventManager = _EM
        sys.modules[name] = m
    sys.modules["core.services.audio.kws.sherpa"] = sherpa_mod

    spec = importlib.util.spec_from_file_location(
        "kws_under_test",
        ROOT / "core/services/audio/kws/__init__.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


class KwsGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls._saved_modules = _load_kws_module()
        cls.kws = cls.mod.KWS

    @classmethod
    def tearDownClass(cls):
        # 恢复被桩覆盖的模块，避免污染同进程内其它测试
        for name, original in cls._saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def setUp(self):
        # 每个用例前复位闸门与会话状态
        self.kws._gate_count = 0
        self.kws._gate_reasons.clear()
        if self.kws._gate_timer is not None:
            self.kws._gate_timer.cancel()
            self.kws._gate_timer = None
        self.kws._conv_paused = False
        self.kws.listen_disabled = False
        self.kws.SherpaOnnx = self.mod.SherpaOnnx if hasattr(self.mod, "SherpaOnnx") else None
        # 复位 fake sherpa 计数
        self.mod.SherpaOnnx.reset_calls = 0

    # ---- 1. 引用计数 ----
    def test_gate_refcount_nested(self):
        t1 = self.kws.gate_on("A", max_seconds=30)
        self.assertTrue(self.kws.paused)
        t2 = self.kws.gate_on("B", max_seconds=30)
        self.assertTrue(self.kws.paused)
        self.assertEqual(self.kws._gate_count, 2)

        self.kws.gate_off(t1)
        self.assertTrue(self.kws.paused, "仍有 1 层闸门，应保持暂停")
        self.kws.gate_off(t2)
        self.assertFalse(self.kws.paused, "全部解除后应恢复")
        self.assertEqual(self.kws._gate_count, 0)

    # ---- 2. 与 pause/resume 正交 ----
    def test_gate_and_conversation_pause_orthogonal(self):
        self.kws.pause()
        self.assertTrue(self.kws.paused)
        tok = self.kws.gate_on("播报")
        self.assertTrue(self.kws.paused)
        self.kws.gate_off(tok)
        self.assertTrue(self.kws.paused, "会话仍暂停")
        self.kws.resume()
        self.assertFalse(self.kws.paused, "两个来源都解除后才恢复")

    def test_conversation_pause_does_not_clear_gate(self):
        tok = self.kws.gate_on("播报")
        self.kws.pause()
        self.kws.resume()  # 闸门仍在
        self.assertTrue(self.kws.paused, "resume 不应清掉仍在生效的闸门")
        self.kws.gate_off(tok)

    # ---- 3. 兜底自动恢复 ----
    def test_gate_autoresume_on_forgotten_off(self):
        self.kws.gate_on("忘记解除", max_seconds=0.2)
        self.assertTrue(self.kws.paused)
        time.sleep(0.5)
        self.assertFalse(self.kws.paused, "超时后应自动恢复，避免 KWS 永久失效")
        self.assertEqual(self.kws._gate_count, 0)

    # ---- 4. 恢复时 reset Sherpa 流 ----
    def test_reset_called_on_gate_release(self):
        self.mod.SherpaOnnx.reset_calls = 0
        tok = self.kws.gate_on("播报")
        self.kws.gate_off(tok)
        self.assertGreaterEqual(
            self.mod.SherpaOnnx.reset_calls, 1,
            "闸门归零时必须 reset Sherpa 流（丢弃播报回声帧）",
        )

    def test_resume_resets_when_no_gate(self):
        self.mod.SherpaOnnx.reset_calls = 0
        self.kws.pause()
        self.kws.resume()
        self.assertGreaterEqual(
            self.mod.SherpaOnnx.reset_calls, 1,
            "resume 恢复监听时应 reset Sherpa 流",
        )

    # ---- 5. listen_disabled 语义不回归 ----
    def test_disable_listening_blocks_resume(self):
        self.kws.disable_listening()
        self.assertTrue(self.kws.paused)
        self.kws.resume()
        self.assertTrue(self.kws.paused, "停止聆听后 resume 不应生效")
        self.kws.enable_listening()
        self.assertFalse(self.kws.paused)

    def test_gate_does_not_override_listen_disabled(self):
        self.kws.disable_listening()
        tok = self.kws.gate_on("播报")
        self.kws.gate_off(tok)
        self.assertTrue(
            self.kws.paused,
            "停止聆听是持久态，闸门解除不应让它恢复",
        )
        self.kws.enable_listening()


if __name__ == "__main__":
    unittest.main()
