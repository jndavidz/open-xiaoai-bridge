"""L3 影子臂（逆向三词模型服务端复现，music-far spec 票 06）测试。

模型产物（D:/_work/data/kws-fw）不在仓库与开发机——测试用**合成小权重**
（随机初始化的小 SoilTDNN）驱动全链路，只锁外部行为：
  1. 权重加载契约（npz 键/形状校验、缺文件报错）；
  2. feed → JSONL：字段集合固定、arm/scale 标识、阈值随行落盘；
  3. 阈值命中语义（低阈值合成权重下可命中、且同段只报一次）；
  4. 异常自动停用且主路无感知（feed 返回 []、enabled=False）；
  5. reset 清段缓冲（同段不重复报）；
  6. 前向与打分口径：与 harness eval_3way 的 Viterbi 语义一致的合成验证。
不依赖 torch、sherpa、设备。
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "kws_l3_shadow", ROOT / "core/services/audio/kws/l3_shadow.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

L3ShadowArm = _mod.L3ShadowArm
L3Forward = _mod.L3Forward
load_l3_weights = _mod.load_l3_weights
word_path_score = _mod.word_path_score
WORD_UNITS = _mod.WORD_UNITS
WORDS3 = _mod.WORDS3
RATE = _mod.RATE

LAYERS = ("a3", "a6", "a9", "a13", "a16", "a19", "a23", "a26", "a29", "a32")


def _write_weights(model_dir: Path, seed: int = 0) -> Path:
    """合成小 SoilTDNN 权重（键名/形状与真实产物一致）。"""
    rng = np.random.default_rng(seed)
    arrays = {}
    shapes = {
        "a3": (96, 200), "a6": (96, 96), "a9": (96, 96),
        "a13": (96, 384), "a16": (96, 96), "a19": (96, 96),
        "a23": (96, 480), "a26": (96, 96), "a29": (96, 96),
        "a32": (30, 96),
    }
    for layer, (out_dim, in_dim) in shapes.items():
        arrays[f"{layer}_W"] = (rng.standard_normal((out_dim, in_dim)) * 0.02).astype(
            np.float32
        )
        arrays[f"{layer}_b"] = np.zeros(out_dim, dtype=np.float32)
    model_dir.mkdir(parents=True, exist_ok=True)
    np.savez(model_dir / "l3_weights.npz", **arrays)
    return model_dir / "l3_weights.npz"


def _speech_like_pcm(seconds: float, seed: int = 3) -> bytes:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    sig = np.zeros_like(t)
    for freq in (200.0, 500.0, 1100.0, 2400.0):
        sig += np.sin(2 * np.pi * freq * t + rng.uniform(0, 6))
    sig = sig / np.max(np.abs(sig)) + 0.05 * rng.standard_normal(len(t))
    return (np.clip(sig, -1, 1) * 32768 * 0.5).astype(np.int16).tobytes()


class LoadWeightsTest(unittest.TestCase):
    def test_valid_npz_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_weights(Path(tmp))
            weights = load_l3_weights(Path(tmp))
            self.assertEqual(weights["a3_W"].shape, (96, 200))
            self.assertEqual(weights["a32_W"].shape, (30, 96))
            self.assertTrue(path.is_file())

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                load_l3_weights(Path(tmp))

    def test_missing_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_weights(Path(tmp))
            z = dict(np.load(Path(tmp) / "l3_weights.npz"))
            del z["a13_W"]
            np.savez(Path(tmp) / "l3_weights.npz", **z)
            with self.assertRaises(ValueError):
                load_l3_weights(Path(tmp))

    def test_bad_shape_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_weights(Path(tmp))
            z = dict(np.load(Path(tmp) / "l3_weights.npz"))
            z["a32_W"] = np.zeros((10, 96), dtype=np.float32)
            np.savez(Path(tmp) / "l3_weights.npz", **z)
            with self.assertRaises(ValueError):
                load_l3_weights(Path(tmp))


class PathScoreTest(unittest.TestCase):
    def test_perfect_units_monotonically_beat_noise(self):
        """合成 ll：词单元高分的段应得到高路径分（Viterbi 语义冒烟）。"""
        rng = np.random.default_rng(0)
        t = 60
        ll_noise = rng.standard_normal((t, 30))
        ll_signal = rng.standard_normal((t, 30))
        units = WORD_UNITS[0]
        for i, unit in enumerate(units):
            ll_signal[i * 5 : i * 5 + 5, unit] = 8.0  # 依次强匹配 8 个单元
        score_signal = word_path_score(ll_signal, 0)
        score_noise = word_path_score(ll_noise, 0)
        self.assertGreater(score_signal, score_noise)

    def test_empty_ll_is_negative_inf(self):
        self.assertEqual(word_path_score(np.zeros((0, 30)), 0), float("-inf"))


class FeedContractTest(unittest.TestCase):
    def _arm(self, tmp: Path, thresholds: dict | None = None) -> L3ShadowArm:
        _write_weights(Path(tmp) / "model")
        return L3ShadowArm(
            model_dir=str(Path(tmp) / "model"),
            thresholds=thresholds,
            log_dir=str(Path(tmp) / "shadow"),
        )

    def _feed_segment(self, arm: L3ShadowArm, pcm: bytes, frame=512 * 2):
        hits = []
        for off in range(0, len(pcm), frame):
            hits += arm.feed(pcm[off : off + frame])
        return hits

    def test_jsonl_fields_fixed_with_arm_and_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            arm = self._arm(Path(tmp), thresholds={"你好小塔": -1e9, "你好助教": 1e9, "小爱同学": 1e9})
            self.assertTrue(arm.enabled)
            self._feed_segment(arm, _speech_like_pcm(2.0))
            arm.reset()
            self._feed_segment(arm, _speech_like_pcm(2.0), frame=512 * 2)
            files = list((Path(tmp) / "shadow").glob("kws-shadow-l3-*.jsonl"))
            self.assertEqual(len(files), 1)
            records = [
                json.loads(line)
                for line in files[0].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(records)
            for record in records:
                for field in (
                    "ts", "arm", "hits", "main_hit", "frames", "model",
                    "thresholds", "scale", "scores",
                ):
                    self.assertIn(field, record)
                self.assertEqual(record["arm"], "l3")
                self.assertEqual(
                    record["scale"], "offline-log-likelihood",
                    "分数量纲必须随行落盘（票 06）",
                )
                self.assertEqual(record["words"], 3)

    def test_low_threshold_hits_once_per_segment(self):
        """极低阈值 → 命中；同段（未 reset）不重复报；reset 后可再报。"""
        with tempfile.TemporaryDirectory() as tmp:
            arm = self._arm(Path(tmp), thresholds={word: -1e9 for word in WORDS3})
            pcm = _speech_like_pcm(2.0)
            hits = self._feed_segment(arm, pcm)
            self.assertTrue(hits, "阈值 -1e9 应命中")
            first_report_len = len(hits)
            hits_again = self._feed_segment(arm, pcm[512 * 2 :])
            for word in hits:
                self.assertNotIn(word, hits_again, "同段未 reset 不得重复报")
            arm.reset()
            hits_fresh = self._feed_segment(arm, pcm)
            self.assertEqual(len(hits_fresh), first_report_len, "reset 后重新可报")

    def test_high_threshold_never_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            arm = self._arm(Path(tmp), thresholds={word: 1e9 for word in WORDS3})
            self.assertEqual(self._feed_segment(arm, _speech_like_pcm(2.0)), [])
            self.assertEqual(arm._hits, 0)

    def test_missing_weights_disables_arm_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            arm = L3ShadowArm(
                model_dir=str(Path(tmp) / "absent"),
                log_dir=str(Path(tmp) / "shadow"),
            )
            self.assertFalse(arm.enabled)
            self.assertEqual(arm.feed(b"\x00\x00" * 512), [], "停用后 feed 恒空")

    def test_runtime_error_disables_and_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            arm = self._arm(Path(tmp))
            self.assertTrue(arm.enabled)
            arm._forward = None  # 注入坏状态模拟运行期异常
            # 段须足够长（≥80 特征帧）才会走到前向
            self.assertEqual(arm.feed(_speech_like_pcm(2.0)), [])
            self.assertFalse(arm.enabled, "运行异常必须自动停用（主路无感知）")

    def test_short_segment_not_scored(self):
        """<80 特征帧的段不出分（不产生 JSONL）。"""
        with tempfile.TemporaryDirectory() as tmp:
            arm = self._arm(Path(tmp), thresholds={word: -1e9 for word in WORDS3})
            self.assertEqual(self._feed_segment(arm, _speech_like_pcm(0.5)), [])
            files = list((Path(tmp) / "shadow").glob("*.jsonl"))
            self.assertEqual(files, [])

    def test_status_reports_scale_and_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            arm = self._arm(Path(tmp))
            status = arm.status()
            self.assertEqual(status["scale"], "offline-log-likelihood")
            self.assertEqual(status["feeds"], 0)
            self.assertEqual(status["arm"], "l3")


if __name__ == "__main__":
    unittest.main()
