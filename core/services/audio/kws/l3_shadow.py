"""L3 影子臂：逆向三词模型（固件线）的服务端复现，只记录、不触发。

music-far spec 票 06。固件侧已训练的三词模型（「小爱同学/你好小塔/你好助教」，
deploy/lx06/firmware-kws/harness/train_3word.py 的 SoilTDNN）以**服务端影子臂**
形态参战：与既有影子臂吃同一份线上音频（strongest 帧），写影子 JSONL，不产生
设备动作、不进 Dialogue、不进生产触发路径——固件投入不必"要么上机要么归零"，
且与软件候选模型做**同口径对抗**（同一段音频）。

推理链忠实移植（numpy，不依赖 torch；权重经 harness/export_l3_npz.py 导出为
`l3_weights.npz` 放配置的 model_dir）：
  PCM → MFCC-40（Kaldi 口径，与 harness/mfcc.py 逐式一致）
      → causal CMVN（滑窗 600 帧，与 train_3word.cmvn_sliding 一致）
      → SoilTDNN 前向（splice/affine/relu/RMS-normalize，组件流同构）
      → 逐词 Viterbi 路径分（[bg* u1..u8 bg*]，与 harness/eval_3way.path_score 逐式一致）
      → 分数 ≥ 该词 threshold ⇒ 记一次 hit（JSONL）

⚠ 分数量纲 = **离线标度**（log-likelihood 域，阈值由 harness 离线标定），
  与 sherpa 引擎真实标度不可比——每条 JSONL 记 `scale` 字段防混读（票 06）。

设计约束（与 ShadowKws 同款）：
  - 只在主 KWS 检测线程内调用；任何异常记 WARNING 并自动停用，绝不影响主路；
  - 段缓冲由主路 480ms 端点 reset 驱动（reset() 清空缓冲与已报标记）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from core.utils.logger import logger

RATE = 16000
# 与 harness WORDS = ['xiaoaai', 'xiaota', 'zhujiao'] 的索引序一一对应
WORDS3 = ("小爱同学", "你好小塔", "你好助教")
# 词单元序（train_3word.WORD_UNITS）；bg 单元 = 10w+{0,3}
WORD_UNITS = {w: [10 * w + u for u in (8, 4, 1, 2, 6, 5, 8, 7)] for w in range(3)}
BG_UNITS = [10 * w + o for w in range(3) for o in (0, 3)]
N_OUT = 30
CMVN_WINDOW = 600
# 单段前向上限（帧）：段由 480ms 端点 reset 界定，此为兜底防异常增长
MAX_SEGMENT_FRAMES = 400  # ≈4s
# 前向频率门（帧）：每积累 ≥16 个新特征帧（≈160ms）才整段重算一次，
# 摊薄 O(段长²) 的重算成本（N3060/NAS 预算）
FORWARD_STRIDE = 16
WEIGHT_KEYS = (
    "a3_W", "a3_b", "a6_W", "a6_b", "a9_W", "a9_b",
    "a13_W", "a13_b", "a16_W", "a16_b", "a19_W", "a19_b",
    "a23_W", "a23_b", "a26_W", "a26_b", "a29_W", "a29_b",
    "a32_W", "a32_b",
)
SPLICE_WINS = {1: [4, -2, -1, 0, 1], 2: [9, -6, -3, 0], 3: [36, -32, -23, -14, -5]}
SPLICE_PADS = {1: (2, 4), 2: (6, 9), 3: (32, 36)}  # (left, right) 累计感受野


# ---- MFCC-40（Kaldi 口径；与 harness/mfcc.py 逐式一致） ----
def _mel_scale(f):
    return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)


def _mel_banks(sr: int) -> np.ndarray:
    num_mel, pad_to = 40, 512
    mel_low = _mel_scale(20.0)
    mel_high = _mel_scale(sr / 2.0 - 200.0)
    delta = (mel_high - mel_low) / (num_mel + 1)
    nbins = pad_to // 2
    w = np.zeros((num_mel, nbins), dtype=np.float64)
    for b in range(num_mel):
        left = mel_low + b * delta
        center = mel_low + (b + 1) * delta
        right = mel_low + (b + 2) * delta
        for i in range(nbins):
            f = i * sr / pad_to
            if left < f < right:
                w[b, i] = ((f - left) / (center - left)) if f <= center else (
                    (right - f) / (right - center)
                )
    return w.astype(np.float32)


def _povey_window(n=400) -> np.ndarray:
    i = np.arange(n, dtype=np.float64)
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * i / n)) ** 0.85


def _dct_matrix(num_ceps=40, num_mel=40) -> np.ndarray:
    m = np.zeros((num_ceps, num_mel), dtype=np.float64)
    for r in range(num_ceps):
        norm = np.sqrt(1.0 / num_mel) if r == 0 else np.sqrt(2.0 / num_mel)
        m[r] = norm * np.cos(np.pi / num_mel * (np.arange(num_mel) + 0.5) * r)
    return m.astype(np.float32)


def _lifter_factors(num_ceps=40, lifter=22.0) -> np.ndarray:
    m = np.arange(num_ceps, dtype=np.float64)
    return (1.0 + 0.5 * lifter * np.sin(np.pi * m / lifter)).astype(np.float32)


class Mfcc40:
    """Kaldi 兼容 MFCC-40（frame 400 / shift 160 / snip_edges / preemph 0.97）。"""

    FRAME_LEN = 400
    FRAME_SHIFT = 160

    def __init__(self, sr: int = RATE):
        self.win = _povey_window()
        self.banks = _mel_banks(sr)
        self.dct = _dct_matrix()
        self.lifter = _lifter_factors()

    def compute(self, x: np.ndarray) -> np.ndarray:
        """1D float PCM（±1 域）→ (T, 40) MFCC。"""
        x = np.asarray(x, dtype=np.float32)
        n = len(x)
        if n < self.FRAME_LEN:
            return np.zeros((0, 40), dtype=np.float32)
        nf = (n - self.FRAME_LEN) // self.FRAME_SHIFT + 1
        idx = np.arange(self.FRAME_LEN)[None, :] + self.FRAME_SHIFT * np.arange(nf)[:, None]
        frames = x[idx].astype(np.float32)
        frames -= frames.mean(axis=1, keepdims=True)
        emph = frames.copy()
        emph[:, 1:] = frames[:, 1:] - 0.97 * frames[:, :-1]
        emph[:, 0] = (1.0 - 0.97) * frames[:, 0]
        emph *= self.win
        spec = np.fft.rfft(emph, n=512, axis=1)
        power = (spec.real**2 + spec.imag**2).astype(np.float32)[:, :256]
        mel = np.maximum(power @ self.banks.T, np.float32(1e-10))
        mfcc = np.log(mel) @ self.dct.T
        mfcc *= self.lifter
        return mfcc.astype(np.float32)


def cmvn_sliding(feats: np.ndarray, window: int = CMVN_WINDOW) -> np.ndarray:
    """causal 滑窗 CMVN（与 train_3word.cmvn_sliding 一致）。"""
    out = np.empty_like(feats)
    for t in range(len(feats)):
        lo = max(0, t - window)
        out[t] = feats[t] - feats[lo : t + 1].mean(axis=0)
    return out


class L3Forward:
    """SoilTDNN numpy 前向（train_3word.SoilTDNN 同构：splice/affine/relu/normalize）。"""

    def __init__(self, weights: dict, eps: float = 1.0):
        self.w = weights
        self.eps = eps

    def _blk(self, h: np.ndarray, name: str) -> np.ndarray:
        h = np.maximum(h @ self.w[f"{name}_W"].T + self.w[f"{name}_b"], 0.0)
        rms = np.sqrt((h * h).mean(axis=1, keepdims=True) + self.eps)
        return h / rms

    @staticmethod
    def _splice(x: np.ndarray, stage: int) -> np.ndarray:
        pl, pr = SPLICE_PADS[stage]
        t = len(x)
        xp = np.pad(x, ((pl, pr), (0, 0)))
        return np.concatenate([xp[pl + d : pl + d + t] for d in SPLICE_WINS[stage]], axis=1)

    def forward(self, feats: np.ndarray) -> np.ndarray:
        """(T, 40) → (T, 30) logits。"""
        h = self._blk(self._splice(feats, 1), "a3")
        h = self._blk(h, "a6")
        h = self._blk(h, "a9")
        h = self._blk(self._splice(h, 2), "a13")
        h = self._blk(h, "a16")
        h = self._blk(h, "a19")
        h = self._blk(self._splice(h, 3), "a23")
        h = self._blk(h, "a26")
        h = self._blk(h, "a29")
        return h @ self.w["a32_W"].T + self.w["a32_b"]


def word_path_score(ll: np.ndarray, widx: int) -> float:
    """逐词 Viterbi 路径分（与 harness eval_3way.path_score 逐式一致）。

    语义：[bg* u1..u8 bg*]，bg 段**零代价等待**（dp 逐帧保留），只有匹配
    词单元时加 ll[t, unit]。ll: (T, 30) = logit + log(30)（eval_3way.ll3_of
    口径，非归一化 log-softmax——阈值按此标度标定，勿改成 log_softmax）。
    """
    t_len = len(ll)
    if t_len == 0:
        return float("-inf")
    units = WORD_UNITS[widx]
    neg = -1e18
    n = len(units)
    dp = np.full(n + 1, neg)
    dp[0] = 0.0
    for t in range(t_len):
        new = dp.copy()
        for s in range(1, n + 1):
            c = dp[s - 1] + ll[t, units[s - 1]]
            if c > new[s]:
                new[s] = c
        dp = new
    return float(dp[n])


def load_l3_weights(model_dir: Path) -> dict:
    """加载并校验 l3_weights.npz（缺键/形状错抛 ValueError）。"""
    path = Path(model_dir) / "l3_weights.npz"
    if not path.is_file():
        raise FileNotFoundError(f"{path} 不存在（先跑 harness/export_l3_npz.py 导出）")
    z = np.load(path)
    weights = {}
    for key in WEIGHT_KEYS:
        if key not in z.files:
            raise ValueError(f"l3_weights.npz 缺键：{key}")
        weights[key] = np.asarray(z[key], dtype=np.float32)
    expected = {"a3_W": (96, 200), "a32_W": (30, 96)}
    for key, shape in expected.items():
        if weights[key].shape != shape:
            raise ValueError(f"l3_weights.npz {key} 形状 {weights[key].shape} ≠ {shape}")
    return weights


class L3ShadowArm:
    """逆向三词模型影子臂（只记录 JSONL，不触发任何动作）。"""

    def __init__(
        self,
        model_dir: str,
        thresholds: dict | None = None,
        log_dir: str | None = None,
        arm: str = "l3",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.arm = arm
        self.words = list(WORDS3)
        # 逐词阈值（离线标度，harness p5 标定）；缺省 = 不命中（保守）
        self.thresholds = {
            word: float((thresholds or {}).get(word, float("inf"))) for word in self.words
        }
        self.enabled = False
        self._disabled_reason = ""
        self._log_dir = Path(log_dir) if log_dir else Path("data/shadow")
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._hits = 0
        self._feeds = 0
        self._forward_runs = 0
        self._last_forward_frame_count = 0
        try:
            self._forward = L3Forward(load_l3_weights(self.model_dir))
            self._mfcc = Mfcc40()
            self._pcm = np.zeros(0, dtype=np.float32)  # 本段 PCM（±1 域）
            self._reported: set[str] = set()
            self._last_scores: dict[str, float] = {}
            self.enabled = True
            logger.info(
                f"[KWS-L3] 已启用：model={self.model_dir.name} "
                f"thresholds={ {k: v for k, v in self.thresholds.items()} }",
                module="KWS",
            )
        except Exception as exc:  # noqa: BLE001 - 影子失败不得影响主 KWS
            self._disable(exc)

    def _disable(self, exc: Exception) -> None:
        self.enabled = False
        self._disabled_reason = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[KWS-L3] 初始化/运行失败，影子停用：{self._disabled_reason}", module="KWS")

    def reset(self) -> None:
        """主路 480ms 端点 reset 时调用：清段缓冲与已报标记。"""
        if not self.enabled:
            return
        self._pcm = np.zeros(0, dtype=np.float32)
        self._last_forward_frame_count = 0
        self._reported.clear()

    def feed(self, frames: bytes, main_hit: bool = False, context: dict | None = None) -> list[str]:
        """喂一帧 16k/单声道/16bit PCM；命中写 JSONL，不触发任何动作。"""
        if not self.enabled or not frames:
            return []
        try:
            self._feeds += 1
            self._pcm = np.concatenate([
                self._pcm,
                np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0,
            ])
            feats = cmvn_sliding(self._mfcc.compute(self._pcm))
            if len(feats) < 80 or len(feats) > MAX_SEGMENT_FRAMES:
                return []  # 太短无意义；超长等端点 reset 兜底
            # 频率门：整段重算是 O(段长²)，只在新增 ≥FORWARD_STRIDE 帧时前向一次
            # （段长被 480ms 端点 reset 界定，峰值成本受 MAX_SEGMENT_FRAMES 上限保护）
            if len(feats) - self._last_forward_frame_count < FORWARD_STRIDE:
                return []
            self._last_forward_frame_count = len(feats)
            # 输出帧 t 需要右侧 36 帧上下文 ⇒ 只对 [0, T-36) 出分
            usable = len(feats) - SPLICE_PADS[3][1]
            if usable < 40:
                return []
            logits = self._forward.forward(feats[:usable])
            self._forward_runs += 1
            # 统一均匀先验（eval_3way.ll3_of 口径：ll = logit - log(1/30)，
            # 非 log_softmax 归一——阈值按此标度标定）
            ll = logits - np.log(np.full(N_OUT, 1.0 / N_OUT))
            scores = {
                word: word_path_score(ll, widx) for widx, word in enumerate(self.words)
            }
            self._last_scores = scores
            hits = []
            for word, score in scores.items():
                threshold = self.thresholds[word]
                if score != float("-inf") and score >= threshold and word not in self._reported:
                    self._reported.add(word)
                    hits.append(word)
            self._hits += len(hits)
            if hits or main_hit:
                self._log(hits, main_hit, context, scores)
            return hits
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)
            return []

    def record_main_hit(self, keyword: str, context: dict | None = None) -> None:
        """记录主 KWS 命中（影子未在本段产出候选命中）——与 ShadowKws 同接口。"""
        if not self.enabled:
            return
        self._log([], True, context, dict(self._last_scores))

    def _log(self, hits, main_hit, context, scores) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "arm": self.arm,
            "hits": hits,
            "main_hit": bool(main_hit),
            "main_keyword": None,
            "frames": self._feeds,
            "model": self.model_dir.name,
            "words": len(self.words),
            "thresholds": {k: v for k, v in self.thresholds.items()},
            # 票 06：分数量纲随结果落盘——离线标度，禁止与 sherpa 引擎分数混读
            "scale": "offline-log-likelihood",
            "scores": {k: (None if v == float("-inf") else round(v, 3)) for k, v in scores.items()},
            "forward_runs": self._forward_runs,
        }
        if context:
            record.update(context)
        path = self._log_dir / f"kws-shadow-l3-{time.strftime('%Y%m%d')}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - 写盘失败仅告警
            logger.warning(f"[KWS-L3] 写日志失败：{exc}", module="KWS")

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "arm": self.arm,
            "disabled_reason": self._disabled_reason,
            "model_dir": str(self.model_dir),
            "words": len(self.words),
            "feeds": self._feeds,
            "hits": self._hits,
            "forward_runs": self._forward_runs,
            "thresholds": dict(self.thresholds),
            "scale": "offline-log-likelihood",
        }
