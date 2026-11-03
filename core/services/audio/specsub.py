"""播放期谱减回声抑制（music-far spec 票 04 的第二原理实现）。

为什么不是线性 AEC：2026-11-03 真机数据实测，块 LMS（256/512/1024 tap、含连续流
100 s 跨段自适应）在真实音乐下 **ERLE 只有 2–3.5 dB**——"5 s 段收敛不足"的假设被
连续流实验推翻，上限来自播放链的 EQ/DRC/限幅非线性，线性时域模型抓不住。
频域谱减不依赖线性时域模型与精确对齐，同一批数据上显著更好：

| 条件 | raw | 块LMS | 谱减 α=0.6 |
|---|---|---|---|
| music_near | 70% | 85% | **90%** |
| music_far  | 10% | 15% | **30%** |

（α ∈ {0.6, 0.8, 1.0, 1.2} 扫描；α ≥ 1.0 开始过减、损伤人声。证据：
`ws: .scratch/lx06-kws-music-far/issues/07` 与 `deploy/lx06/kws-lx06*/eval_specsub.json`。）

算法（单通道谱减，STFT 域）：
    |S(f)| = max(|M(f)| − α·|R(f)|, |M(f)|·10^(floor_db/20))
    S(f)   = |S(f)| · e^{j∠M(f)}          （保留近端相位）
    s      = ISTFT_OLA(S)

设计约束（与 aec.py 一致）：
  - 纯 numpy，多声道交错 int16 进、1ch int16 出，接口 feed/reset/stats；
  - 参考声道来自 `audio_input.reference_channels`（LX06 8ch = ch6）；
  - 参考全零（非播放期）/ 参考恒定 → 静默直通，不抛错；
  - 延迟预算 = 一帧 STFT（默认 512 样本 ≈ 32 ms），stats() 如实报告；
  - 默认关闭，由 `wakeup_channel.playing="specsub"` 或 aec.mode 启用。
"""

from __future__ import annotations

import numpy as np

RATE = 16000
# STFT 参数：512/160 = 32 ms 窗、10 ms 帧移。窗口越大频率分辨率越好但延迟越高；
# 32 ms 与 KWS 的 chunk 粒度同量级，实测最优。
N_FFT = 512
HOP = 160
# 谱减强度与下限（2026-11-03 扫描最优：α=0.6 时 near 90% / far 30%）
ALPHA = 0.6
FLOOR_DB = -20.0
# 参考相对麦克风的延迟（LX06 实测 3.9–4.1 ms @16k ≈ 62–66 样本）
REF_LEAD_SAMPLES = 64
# 参考"活跃"判据：整帧 RMS 低于该值视为非播放期（S16 量纲）
REF_ACTIVE_RMS = 30.0


class SpectralSuppressor:
    """谱减回声抑制：多声道交错 int16 PCM 进，1ch（抑制后）int16 出。"""

    def __init__(
        self,
        channels: int = 8,
        ref_channel: int = 6,
        mic_channel: int | None = None,
        alpha: float = ALPHA,
        floor_db: float = FLOOR_DB,
        ref_lead_samples: int = REF_LEAD_SAMPLES,
    ) -> None:
        if int(channels) < 2:
            raise ValueError("channels must be >= 2（谱减需要参考声道）")
        self.channels = int(channels)
        if not 0 <= int(ref_channel) < self.channels:
            raise ValueError(f"ref_channel {ref_channel} 越界（channels={channels}）")
        self.ref_channel = int(ref_channel)
        self.mic_channel = None if mic_channel is None else int(mic_channel)
        if self.mic_channel is not None:
            if not 0 <= self.mic_channel < self.channels:
                raise ValueError(f"mic_channel {mic_channel} 越界")
            if self.mic_channel == self.ref_channel:
                raise ValueError("mic_channel 不能等于 ref_channel")
        self.alpha = max(0.0, float(alpha))
        self.floor = float(10.0 ** (float(floor_db) / 20.0))
        self.ref_lead = max(0, int(ref_lead_samples))

        self._win = np.hanning(N_FFT).astype(np.float64)
        self._reset_state()

    # ---- 状态 ----

    def _reset_state(self) -> None:
        self._mic_buf = np.zeros(0, dtype=np.float64)   # 待处理近端样本
        self._ref_buf = np.zeros(0, dtype=np.float64)   # 待处理参考样本（已延迟）
        self._ref_hist = np.zeros(self.ref_lead, dtype=np.float64)  # 对齐线
        self._ola = np.zeros(N_FFT, dtype=np.float64)   # 重叠相加累加
        self._ola_norm = np.zeros(N_FFT, dtype=np.float64)
        self._counters = {
            "frames": 0,
            "bypass_no_ref": 0,
            "bypass_ref_flat": 0,
        }
        self._erle_num = 1e-9
        self._erle_den = 1e-9

    def reset(self) -> None:
        self._reset_state()

    def stats(self) -> dict:
        out = dict(self._counters)
        out["erle_db"] = round(float(10.0 * np.log10(self._erle_num / self._erle_den)), 2)
        out["latency_ms"] = round(N_FFT * 1000.0 / RATE, 1)
        out["alpha"] = self.alpha
        out["floor_db"] = round(20.0 * float(np.log10(self.floor)), 1)
        out["ref_channel"] = self.ref_channel
        return out

    # ---- 输入解析 ----

    def _split(self, pcm: bytes) -> tuple[np.ndarray, np.ndarray] | None:
        frame_bytes = self.channels * 2
        usable = len(pcm) - len(pcm) % frame_bytes
        if usable <= 0:
            return None
        a = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float64)
        a = a.reshape(-1, self.channels)
        if self.mic_channel is None:
            # 未指定则取全部「非参考」声道的等权平均
            cols = [c for c in range(self.channels) if c != self.ref_channel]
            mic = a[:, cols].mean(axis=1)
        else:
            mic = a[:, self.mic_channel]
        ref = a[:, self.ref_channel]
        return mic, ref

    def _align_ref(self, ref: np.ndarray) -> np.ndarray:
        """把参考延迟 ref_lead 样本，使其与麦克风同一时刻对齐。"""
        if self.ref_lead == 0:
            return ref
        cat = np.concatenate([self._ref_hist, ref])
        self._ref_hist = cat[-self.ref_lead:]
        return cat[: -self.ref_lead] if self.ref_lead else cat

    # ---- 核心 ----

    def _process_frame(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        M = np.fft.rfft(mic * self._win)
        R = np.fft.rfft(ref * self._win)
        mag_m = np.abs(M)
        mag_r = np.abs(R)
        mag_s = np.maximum(mag_m - self.alpha * mag_r, mag_m * self.floor)
        S = mag_s * np.exp(1j * np.angle(M))
        return np.fft.irfft(S)

    def feed(self, pcm: bytes) -> bytes:
        split = self._split(pcm)
        if split is None:
            return b""
        mic, ref = split
        aligned = self._align_ref(ref)

        total = float((aligned ** 2).mean()) if aligned.size else 0.0
        if total <= 0.0:
            self._counters["bypass_no_ref"] += 1
            out = mic
        else:
            # 「参考恒定」判据：DC 分量占比 > 99% 视为无效参考。
            # 不用方差——延迟对齐会让前 ref_lead 个样本为 0，使常数序列的方差虚高；
            # DC 本身不携带可消的回声（落在 STFT 第 0 bin），直通更安全。
            mean = float(aligned.mean())
            if (mean * mean) / total > 0.99:
                self._counters["bypass_ref_flat"] += 1
                out = mic
            else:
                self._mic_buf = np.concatenate([self._mic_buf, mic])
                self._ref_buf = np.concatenate([self._ref_buf, aligned])
                chunks = []
                while self._mic_buf.size >= N_FFT:
                    m = self._mic_buf[:N_FFT]
                    r = self._ref_buf[:N_FFT]
                    frame = self._process_frame(m, r)
                    self._ola += frame * self._win
                    self._ola_norm += self._win ** 2
                    chunks.append(self._ola[:HOP] / np.maximum(self._ola_norm[:HOP], 1e-8))
                    self._ola = np.concatenate([self._ola[HOP:], np.zeros(HOP)])
                    self._ola_norm = np.concatenate([self._ola_norm[HOP:], np.zeros(HOP)])
                    self._mic_buf = self._mic_buf[HOP:]
                    self._ref_buf = self._ref_buf[HOP:]
                    self._counters["frames"] += 1
                    self._erle_num += float((m ** 2).sum())
                    self._erle_den += float((frame[:HOP] ** 2).sum()) + 1e-9
                if chunks:
                    out = np.concatenate(chunks)
                else:
                    out = np.zeros(0)
        if out.size == 0:
            return b""
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
