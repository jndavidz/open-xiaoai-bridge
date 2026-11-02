"""VAD 端点语义测试（music-far spec 票 02）。

只测外部行为：给一帧 PCM + 一个 VAD prob，断言「KWS 是否收到帧」「检测流
是否被 reset」「暂停态是否完全不进」。sherpa/audio 用桩替身（复用
test_kws_gate 的加载器），不依赖模型、NAS 或设备。

锁定行为：
  1. endpoint（默认）：VAD 判非语音的帧仍进 KWS（全帧喂入，可观察可计数）；
  2. 静音 480ms 仍触发检测流 reset（会话切分与回声丢弃依赖它）；
  3. 暂停态（会话暂停 / 播放闸门 / 停止聆听 / 会话中）不进 KWS；
  4. gate（回滚模式）：旧行为原样——非语音帧不进 KWS；
  5. 非法配置回落 endpoint。
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_kws_gate import _load_kws_module  # noqa: E402

FRAME = b"\x00\x01" * 512


class VadEndpointTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls._saved = _load_kws_module()
        cls.kws = cls.mod.KWS

    @classmethod
    def tearDownClass(cls):
        for name, original in cls._saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    def setUp(self):
        kws = self.kws
        kws._gate_count = 0
        kws._gate_reasons.clear()
        if kws._gate_timer is not None:
            kws._gate_timer.cancel()
            kws._gate_timer = None
        kws._conv_paused = False
        kws.listen_disabled = False
        kws.vad_active = False
        kws.vad_silence_frames = 0
        kws._kws_frames_fed = 0
        kws._kws_nonspeech_fed = 0
        kws._vad_resets = 0
        self.mod.SherpaOnnx.reset_calls = 0
        # kws 调用记录：[(frames,)]；返回值按 kws_results 队列弹出
        self.kws_calls = []
        self.kws_results = []
        self.mod.SherpaOnnx.kws = self._fake_kws
        # 命中派发打桩（真实实现走 EventManager/asyncio，与本测试无关）
        self.kws.on_message = lambda text: None
        # 默认桩：无会话（非会话中）；个别用例可覆盖 get_xiaozhi
        self.mod.get_xiaozhi = lambda: None

    def _fake_kws(self, frames):
        self.kws_calls.append(frames)
        if self.kws_results:
            return self.kws_results.pop(0)
        return None

    def _step(self, prob, frame=FRAME):
        self.mod.Silero.vad = lambda *a, **k: prob
        return self.kws._process_frame(frame, b"")


class EndpointModeTest(VadEndpointTestBase):
    def setUp(self):
        super().setUp()
        self.kws.vad_mode = "endpoint"

    def test_nonspeech_frames_still_reach_kws(self):
        """票 02 核心：VAD 判非语音的帧仍进 KWS。"""
        hit = self._step(prob=0.0)  # 远低于阈值 0.2
        self.assertIsNone(hit)
        self.assertEqual(len(self.kws_calls), 1, "非语音帧必须仍进 KWS")
        counters = self.kws.kws_counters()
        self.assertEqual(counters["frames_fed"], 1)
        self.assertEqual(counters["nonspeech_fed"], 1, "非语音帧计数应递增")

    def test_every_frame_reaches_kws_across_silence(self):
        """连续静音帧全部进 KWS（此前 gate 模式把它们全部丢弃）。"""
        for _ in range(10):
            self._step(prob=0.0)
        self.assertEqual(len(self.kws_calls), 10)
        self.assertEqual(self.kws.kws_counters()["nonspeech_fed"], 10)

    def test_silence_480ms_triggers_stream_reset(self):
        """静音超过 min_silence（默认 480ms = 15 帧）仍触发检测流 reset。"""
        for _ in range(15):
            self._step(prob=0.0)
        self.assertEqual(self.mod.SherpaOnnx.reset_calls, 0, "未到 480ms 不应 reset")
        self._step(prob=0.0)  # 第 16 帧（480ms）
        self.assertEqual(
            self.mod.SherpaOnnx.reset_calls, 1, "480ms 静音必须 reset 检测流"
        )
        self.assertEqual(self.kws.kws_counters()["vad_resets"], 1)

    def test_brief_silence_gap_does_not_reset(self):
        """语句中短暂停顿（<480ms）不 reset，保持检测流连续。"""
        self._step(prob=0.9)  # 语音激活
        for _ in range(10):  # 320ms 静音间隙
            self._step(prob=0.0)
        self.assertEqual(self.mod.SherpaOnnx.reset_calls, 0)
        self._step(prob=0.9)  # 语音恢复
        self.assertEqual(self.mod.SherpaOnnx.reset_calls, 0)

    def test_hit_resets_vad_state(self):
        """命中后 VAD 状态复位（下一句重新起算）。"""
        self.kws_results.append({"keyword": "你好小塔", "tokens": [], "timestamps": []})
        hit = self._step(prob=0.9)
        self.assertEqual(hit["keyword"], "你好小塔")
        self.assertFalse(self.kws.vad_active)
        self.assertEqual(self.kws.vad_silence_frames, 0)

    def test_paused_state_never_reaches_kws(self):
        """暂停态不进：会话暂停 / 停止聆听 / 播放闸门 / 会话中。"""
        # 会话暂停
        self.kws.pause()
        self._step(prob=0.9)
        self.assertEqual(len(self.kws_calls), 0)
        self.kws.resume()
        # 停止聆听
        self.kws.disable_listening()
        self._step(prob=0.9)
        self.assertEqual(len(self.kws_calls), 0)
        self.kws.enable_listening()
        # 播放闸门
        token = self.kws.gate_on("播报")
        self._step(prob=0.9)
        self.assertEqual(len(self.kws_calls), 0)
        self.kws.gate_off(token)
        # 会话中（device_state = LISTENING）
        self.mod.get_xiaozhi = lambda: SimpleNamespace(
            device_state=self.mod.DeviceState.LISTENING
        )
        self._step(prob=0.9)
        self.assertEqual(len(self.kws_calls), 0, "会话中（LISTENING）不应进 KWS")
        # 恢复后正常进
        self.mod.get_xiaozhi = lambda: None
        self._step(prob=0.9)
        self.assertEqual(len(self.kws_calls), 1)


class GateModeRollbackTest(VadEndpointTestBase):
    """gate = 旧实现原样保留（回滚模式）：非语音帧不进 KWS。"""

    def setUp(self):
        super().setUp()
        self.kws.vad_mode = "gate"

    def test_nonspeech_frames_do_not_reach_kws(self):
        self._step(prob=0.0)
        self.assertEqual(len(self.kws_calls), 0, "gate 模式非语音帧不得进 KWS")
        # 语音帧仍进
        self._step(prob=0.9)
        self.assertEqual(len(self.kws_calls), 1)

    def test_hangover_silence_frames_reach_kws(self):
        """激活后的静音 hangover（≤min_silence）仍进 KWS（旧行为）。"""
        self._step(prob=0.9)
        for _ in range(5):
            self._step(prob=0.0)
        self.assertEqual(len(self.kws_calls), 6)
        # hangover 结束（>480ms）→ reset
        for _ in range(20):
            self._step(prob=0.0)
        self.assertEqual(self.mod.SherpaOnnx.reset_calls, 1)


class ConfigContractTest(VadEndpointTestBase):
    def test_invalid_mode_falls_back_to_endpoint(self):
        self.kws.vad_mode = "always-on"
        self.kws.apply_runtime_config.__self__.config_manager = SimpleNamespace(
            get_app_config=lambda path, default=None: (
                {"vad_mode": "always-on"} if path == "kws" else default
            )
        )
        self.kws.apply_runtime_config()
        self.assertEqual(self.kws.vad_mode, "endpoint")

    def test_gate_mode_from_config(self):
        self.kws.config_manager = SimpleNamespace(
            get_app_config=lambda path, default=None: (
                {"vad_mode": "gate"} if path == "kws" else default
            )
        )
        self.kws.apply_runtime_config()
        self.assertEqual(self.kws.vad_mode, "gate")

    def test_default_mode_is_endpoint(self):
        self.kws.config_manager = SimpleNamespace(
            get_app_config=lambda path, default=None: default
        )
        self.kws.apply_runtime_config()
        self.assertEqual(self.kws.vad_mode, "endpoint")


if __name__ == "__main__":
    unittest.main()
