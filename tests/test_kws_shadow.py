"""影子 KWS 纯逻辑测试（不加载模型）。

覆盖：现网/候选模型文件发现、缺失文件报错、影子日志 JSONL 格式、
命中留证 PCM 快照（环形缓冲/清理/vad 窗口最大值）、shadow context 字段来源。
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

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

    def test_log_records_main_hit_without_shadow_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            shadow = object.__new__(ShadowKws)
            shadow._log_dir = Path(tmp)
            shadow._frames = 7
            shadow._hits = 0
            shadow._log([], main_hit=True)
            record = json.loads(
                next(Path(tmp).glob("kws-shadow-*.jsonl")).read_text(encoding="utf-8")
            )
            self.assertEqual(record["hits"], [])
            self.assertTrue(record["main_hit"])

    def test_log_includes_arm_context_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            shadow = object.__new__(ShadowKws)
            shadow._log_dir = Path(tmp)
            shadow._frames = 3
            shadow._hits = 0
            shadow.arm = "ch0"
            shadow.model_dir = Path("shadow")
            shadow.words = ["你好小塔"]
            shadow.score = 1.0
            shadow.threshold = 0.25
            shadow.max_active_paths = 16
            shadow._cpu_ms = 2.5
            shadow._audio_ms = 1000.0
            shadow._rtf = 0.0025
            shadow._log(
                [],
                True,
                context={"paused": True, "gate_count": 1},
                main_keyword="你好小塔",
                cpu_ms=2.5,
            )
            record = json.loads(
                next(Path(tmp).glob("kws-shadow-*.jsonl")).read_text(encoding="utf-8")
            )
            self.assertEqual(record["arm"], "ch0")
            self.assertEqual(record["main_keyword"], "你好小塔")
            self.assertEqual(record["max_active_paths"], 16)
            self.assertEqual(record["paused"], True)
            self.assertEqual(record["gate_count"], 1)
            self.assertEqual(record["cpu_ms"], 2.5)


class ShadowSnapshotTest(unittest.TestCase):
    """命中留证：_log 落 PCM 快照、路径写进 JSONL、环形缓冲用后即清。"""

    def _make(self, tmp: Path, arm: str = "strongest"):
        shadow = object.__new__(ShadowKws)
        shadow.enabled = True
        shadow.arm = arm
        shadow._disabled_reason = ""
        shadow._log_dir = tmp / "shadow"
        shadow._log_dir.mkdir(parents=True, exist_ok=True)
        shadow._snapshot_dir = tmp / "snaps"
        shadow._snapshot_dir.mkdir(parents=True, exist_ok=True)
        shadow._ring = []
        shadow._ring_max_bytes = 96000
        shadow._vad_prob_max = None
        shadow._snapshots_written = 0
        shadow._frames = 0
        shadow._hits = 0
        shadow._cpu_ms = 0.0
        shadow._audio_ms = 0.0
        shadow._rtf = 0.0
        shadow.model_dir = Path("shadow-model")
        shadow.words = ["你好小塔"]
        shadow.score = 1.0
        shadow.threshold = 0.25
        shadow.max_active_paths = 16
        return shadow

    def test_log_dumps_snapshot_and_records_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            shadow._ring = [b"\x01\x02", b"\x03\x04"]
            shadow._log(["你好小塔"], False, context={"vad_prob": 0.5})
            pcm = list((tmp / "snaps").glob("shadow_strongest_*.pcm"))
            self.assertEqual(len(pcm), 1)
            self.assertEqual(pcm[0].read_bytes(), b"\x01\x02\x03\x04")
            record = json.loads(
                next((tmp / "shadow").glob("kws-shadow-*.jsonl")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record["snapshot"], str(pcm[0]))
            self.assertEqual(shadow._ring, [])
            self.assertEqual(shadow._snapshots_written, 1)

    def test_snapshot_cleanup_keeps_recent_per_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            for i in range(_shadow.SNAPSHOT_KEEP + 5):
                (tmp / "snaps" / f"shadow_strongest_{i:012d}.pcm").write_bytes(b"x")
            # 别的 arm / 主路文件不受清理影响
            (tmp / "snaps" / "shadow_ch0_999999.pcm").write_bytes(b"x")
            (tmp / "snaps" / "kws_hit_999999.pcm").write_bytes(b"x")
            shadow._ring = [b"\x01"]
            shadow._log(["你好小塔"], False)
            snaps = sorted((tmp / "snaps").glob("shadow_strongest_*.pcm"))
            self.assertEqual(len(snaps), _shadow.SNAPSHOT_KEEP)
            self.assertTrue(snaps[0].name > "shadow_strongest_000000000004")
            self.assertTrue((tmp / "snaps" / "shadow_ch0_999999.pcm").exists())
            self.assertTrue((tmp / "snaps" / "kws_hit_999999.pcm").exists())

    def test_main_hit_also_dumps_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp, arm="ch0")
            shadow._ring = [b"\xaa\xbb"]
            shadow._log([], True, main_keyword="你好小塔")
            pcm = list((tmp / "snaps").glob("shadow_ch0_*.pcm"))
            self.assertEqual(len(pcm), 1)
            record = json.loads(
                next((tmp / "shadow").glob("kws-shadow-*.jsonl")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(record["main_hit"])
            self.assertEqual(record["snapshot"], str(pcm[0]))

    def test_no_snapshot_when_ring_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            shadow._log(["你好小塔"], False)
            self.assertEqual(list((tmp / "snaps").iterdir()), [])
            record = json.loads(
                next((tmp / "shadow").glob("kws-shadow-*.jsonl")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertNotIn("snapshot", record)

    def test_vad_prob_is_window_max_then_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            shadow._vad_prob_max = 0.82
            shadow._log(["你好小塔"], False, context={"vad_prob": 0.11})
            record = json.loads(
                next((tmp / "shadow").glob("kws-shadow-*.jsonl")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record["vad_prob"], 0.82)
            self.assertIsNone(shadow._vad_prob_max)
            # 复位后无累计值：回退到 context 的当前帧值
            log_file = next((tmp / "shadow").glob("kws-shadow-*.jsonl"))
            shadow._log([], True, context={"vad_prob": 0.3})
            record2 = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(record2["vad_prob"], 0.3)

    def test_feed_appends_ring_and_tracks_vad_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            shadow._stream = SimpleNamespace(accept_waveform=lambda rate, samples: None)

            class _NoHitKws:
                def is_ready(self, stream):
                    return False

            shadow._kws = _NoHitKws()
            frames = b"\x00\x01" * 512
            hits = shadow.feed(frames, context={"vad_prob": 0.42})
            self.assertEqual(hits, [])
            self.assertEqual(shadow._ring, [frames])
            self.assertAlmostEqual(shadow._vad_prob_max, 0.42)
            # 第二帧更高值覆盖
            shadow.feed(frames, context={"vad_prob": 0.9})
            self.assertAlmostEqual(shadow._vad_prob_max, 0.9)

    def test_feed_hit_writes_snapshot_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            shadow._stream = SimpleNamespace(accept_waveform=lambda rate, samples: None)

            class _HitKws:
                def __init__(self):
                    self.calls = 0

                def is_ready(self, stream):
                    self.calls += 1
                    return self.calls == 1  # 真实 sherpa reset 后不再 ready

                def decode_stream(self, stream):
                    pass

                def get_result(self, stream):
                    return SimpleNamespace(keyword="你好小塔")

                def reset_stream(self, stream):
                    pass

            kws_stub = _HitKws()
            shadow._kws = kws_stub
            frames = b"\x00\x01" * 512
            hits = shadow.feed(frames, context={"vad_prob": 0.9})
            self.assertEqual(hits, ["你好小塔"])
            pcm = list((tmp / "snaps").glob("shadow_strongest_*.pcm"))
            self.assertEqual(len(pcm), 1)
            self.assertEqual(pcm[0].read_bytes(), frames)
            record = json.loads(
                next((tmp / "shadow").glob("kws-shadow-*.jsonl")).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record["snapshot"], str(pcm[0]))
            self.assertEqual(shadow._vad_prob_max, None)

    def test_status_exposes_snapshot_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            shadow = self._make(tmp)
            status = shadow.status()
            self.assertEqual(status["snapshots"], 0)
            self.assertEqual(status["snapshot_dir"], str(tmp / "snaps"))


class ShadowContextTest(unittest.TestCase):
    """shadow context 字段来源：device_state 取 MainApp、playing 取 speaker、ch0 计数。"""

    _CTX_STUB_NAMES = [
        "core.services.audio.kws.sherpa",
        "core.ref",
        "core.services.protocols.typing",
        "core.utils.config",
        "core.utils.logger",
        "core.services.audio.stream",
        "core.services.audio.vad.silero",
        "core.wakeup_session",
    ]

    @classmethod
    def setUpClass(cls):
        cls._saved = {n: sys.modules.get(n) for n in cls._CTX_STUB_NAMES}
        for name in cls._CTX_STUB_NAMES:
            sys.modules[name] = types.ModuleType(name)
        ref = sys.modules["core.ref"]
        ref.set_kws = lambda k: None
        ref.get_app = lambda: cls._app
        ref.get_xiaoai = lambda: None
        ref.get_xiaozhi = lambda: cls._xiaozhi
        ref.get_speaker = lambda: cls._speaker
        ref.get_kws = lambda: None
        typing = sys.modules["core.services.protocols.typing"]
        typing.AudioConfig = SimpleNamespace(FORMAT="int16", FRAME_SIZE=512)
        typing.DeviceState = SimpleNamespace(
            IDLE="idle", LISTENING="listening", SPEAKING="speaking"
        )
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
        logmod = sys.modules["core.utils.logger"]

        class _L:
            def __getattr__(self, _n):
                return lambda *a, **k: None

        logmod.logger = _L()
        stream_mod = sys.modules["core.services.audio.stream"]
        stream_mod.MyAudio = SimpleNamespace(create=lambda: None)
        stream_mod.GlobalStreamCh0 = SimpleNamespace()
        sys.modules["core.services.audio.kws.sherpa"].SherpaOnnx = SimpleNamespace(
            start=lambda: None
        )
        sys.modules["core.services.audio.vad.silero"].Silero = SimpleNamespace(
            vad=lambda *a, **k: 0.0
        )

        class _EM:
            @staticmethod
            async def wakeup(*a, **k):
                return None

        sys.modules["core.wakeup_session"].EventManager = _EM
        spec = importlib.util.spec_from_file_location(
            "kws_under_test_ctx", ROOT / "core/services/audio/kws/__init__.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.mod = module
        cls.kws = module.KWS

    @classmethod
    def tearDownClass(cls):
        for name, original in cls._saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    _app = None
    _xiaozhi = None
    _speaker = None

    def setUp(self):
        ShadowContextTest._app = None
        ShadowContextTest._xiaozhi = None
        ShadowContextTest._speaker = None
        self.kws._ch0_reads = 10
        self.kws._ch0_short = 2
        # listen_disabled / _conv_paused 在 start() 才赋值，测试单例未启动
        self.kws.listen_disabled = False
        self.kws._conv_paused = False
        self.kws._gate_count = 0
        self.kws._gate_reasons = []

    def test_context_device_state_from_app(self):
        ShadowContextTest._app = SimpleNamespace(device_state="speaking")
        context = self.kws._shadow_context(None, 0.4, False)
        self.assertEqual(context["device_state"], "speaking")
        self.assertIsNone(context["playing"])

    def test_context_device_state_falls_back_to_xiaozhi(self):
        xiaozhi_stub = SimpleNamespace(device_state="listening")
        context = self.kws._shadow_context(xiaozhi_stub, 0.4, False)
        self.assertEqual(context["device_state"], "listening")

    def test_context_playing_from_speaker(self):
        ShadowContextTest._speaker = SimpleNamespace(status="playing")
        context = self.kws._shadow_context(None, 0.4, False)
        self.assertEqual(context["playing"], "playing")
        self.assertIsNone(context["device_state"])

    def test_context_ch0_counters(self):
        context = self.kws._shadow_context(None, 0.4, False)
        self.assertEqual(context["ch0_reads"], 10)
        self.assertEqual(context["ch0_short"], 2)

    def test_resolve_device_state_prefers_app(self):
        ShadowContextTest._app = SimpleNamespace(device_state="idle")
        ShadowContextTest._xiaozhi = SimpleNamespace(device_state="listening")
        self.assertEqual(self.kws._resolve_device_state(None), "idle")
        self.assertEqual(self.kws._resolve_device_state(object()), "idle")
        ShadowContextTest._app = None
        self.assertEqual(self.kws._resolve_device_state(object()), None)


if __name__ == "__main__":
    unittest.main()
