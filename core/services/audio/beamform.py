"""六麦阵列延迟-求和（DAS）波束成形。

离线验证用实现（numpy）。接口与 `ChannelDownmixer` 的降声道策略一致：
6ch 交错 int16 PCM 进、1ch 交错 int16 PCM 出，流式 `feed` / `reset`。

阵列几何来自固件库内常量（正六边形、半径 3.50 cm、共面、每 60° 一个）：
`docs/devices/lx06/mic-array-6ch-capture.md` §6.5。**通道→几何点的顺序
是假设值**（ch0 = mic1 起顺时针），尚未用真机脉冲响应验证；离线评测工具
会把 DOA 估计的通道时延与几何预测做残差对照，用来暴露映射错误。
"""

from __future__ import annotations

import collections

import numpy as np

RATE = 16000
SOUND_SPEED_MPS = 343.0
MIC_RADIUS_M = 0.035
# 机身最大通道间距 70 mm ≈ 3.27 样本；搜索窗留 4，超过 3.5 视为不可信。
MAX_LAG_SAMPLES = 4
PHYSICAL_MAX_LAG = 3.5
HISTORY_SAMPLES = 16
DOA_MIN_RMS = 0.005  # ≈ -46 dBFS，滤掉 -55~-59 dBFS 的常态底噪
DOA_STEP_LIMIT = 0.5
DOA_DEADBAND = 0.25

# ch0..ch5 → 正六边形顶点（米，x 右 / y 正前），ch0=(0,+0.035) 起顺时针。
MIC_COORDS_M = np.array(
    [
        [0.0, MIC_RADIUS_M],
        [MIC_RADIUS_M * np.cos(np.pi / 6), MIC_RADIUS_M * np.sin(np.pi / 6)],
        [MIC_RADIUS_M * np.cos(np.pi / 6), -MIC_RADIUS_M * np.sin(np.pi / 6)],
        [0.0, -MIC_RADIUS_M],
        [-MIC_RADIUS_M * np.cos(np.pi / 6), -MIC_RADIUS_M * np.sin(np.pi / 6)],
        [-MIC_RADIUS_M * np.cos(np.pi / 6), MIC_RADIUS_M * np.sin(np.pi / 6)],
    ],
    dtype=np.float64,
)


def steering_delays(angle_deg: float, rate: int = RATE) -> np.ndarray:
    """平面波从 `angle_deg` 到达时，各通道相对阵列中心的到达提前量（样本）。

    0° = 正前方，+90° = 右。正值为提前到达；对齐时按该值延迟。
    """
    theta = np.deg2rad(angle_deg)
    direction = np.array([np.sin(theta), np.cos(theta)])
    return MIC_COORDS_M @ direction / SOUND_SPEED_MPS * rate


class DelayAndSum:
    """固定时延的延迟-求和。`delays_samples` 为各通道到达提前量（相对参考）。"""

    def __init__(self, delays_samples, channels: int = 6) -> None:
        self.channels = int(channels)
        self._history = np.zeros((HISTORY_SAMPLES, self.channels), dtype=np.float32)
        self._int_delays = None
        self._frac_delays = None
        self.set_delays(delays_samples)

    def set_delays(self, delays_samples) -> None:
        delays = np.asarray(delays_samples, dtype=np.float64)
        if delays.shape != (self.channels,):
            raise ValueError("delays_samples 长度必须等于通道数")
        # 加统一延迟让所有通道非负（不影响指向，只引入固定组延迟）。
        delays = delays - float(np.min(delays))
        int_delays = np.floor(delays).astype(int)
        if int(np.max(int_delays)) > HISTORY_SAMPLES - 2:
            raise ValueError("delay exceeds history length")
        self._delays = delays
        self._int_delays = int_delays
        self._frac_delays = delays - int_delays

    def reset(self) -> None:
        self._history[:] = 0.0

    def feed(self, samples: np.ndarray) -> np.ndarray:
        """samples: (n, channels) float；返回 (n,) float。"""
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError("samples 形状必须为 (n, channels)")
        n = len(samples)
        if n == 0:
            return np.zeros(0, dtype=np.float32)

        buffer = np.concatenate([self._history, samples], axis=0)
        out = np.zeros(n, dtype=np.float32)
        for channel in range(self.channels):
            offset = HISTORY_SAMPLES - int(self._int_delays[channel])
            index = np.arange(n) + offset
            frac = self._frac_delays[channel]
            out += (1.0 - frac) * buffer[index, channel] + frac * buffer[index - 1, channel]
        out /= self.channels
        self._history = buffer[-HISTORY_SAMPLES:].copy()
        return out


def _gcc_phat_lag(x: np.ndarray, reference: np.ndarray, max_lag: int) -> float | None:
    """估计 `x` 相对 `reference` 的时延（样本）；不可信时返回 None。"""
    n = len(x)
    window = np.hanning(n)
    spectrum = np.fft.rfft(x * window)
    ref_spectrum = np.fft.rfft(reference * window)
    cross = spectrum * np.conj(ref_spectrum)
    cross /= np.abs(cross) + 1e-10
    cc = np.fft.irfft(cross, n=n)
    values = np.concatenate([cc[-max_lag:], cc[: max_lag + 1]])
    lags = np.arange(-max_lag, max_lag + 1)
    index = int(np.argmax(np.abs(values)))
    if abs(values[index]) < 3.0 * (np.median(np.abs(values)) + 1e-12):
        return None
    lag = float(lags[index])
    if abs(lag) > PHYSICAL_MAX_LAG:
        return None
    if 0 < index < len(values) - 1:
        left, centre, right = values[index - 1], values[index], values[index + 1]
        denom = left - 2.0 * centre + right
        if abs(denom) > 1e-12:
            delta = 0.5 * (left - right) / denom
            if abs(delta) <= 0.5:
                lag += delta
    return lag


class GccPhatDoa:
    """GCC-PHAT 声源方向估计：逐对（ch_i vs ch0）求时延，多帧取中位数。"""

    def __init__(
        self,
        channels: int = 6,
        frame_len: int = 1024,
        max_lag: int = MAX_LAG_SAMPLES,
        smooth_frames: int = 7,
        min_rms: float = DOA_MIN_RMS,
    ) -> None:
        self.channels = int(channels)
        self.frame_len = max(512, int(frame_len))
        self.max_lag = int(max_lag)
        self.min_rms = float(min_rms)
        self._history = collections.deque(maxlen=max(1, int(smooth_frames)))

    def reset(self) -> None:
        self._history.clear()

    def update(self, samples: np.ndarray) -> np.ndarray | None:
        """返回各通道到达提前量（样本，相对 ch0）；不可信返回 None。"""
        samples = np.asarray(samples, dtype=np.float64)
        if len(samples) < 2:
            return None
        frame = samples[-self.frame_len :]
        if float(np.sqrt(np.mean(frame**2))) < self.min_rms:
            return None
        lags = [0.0]
        for channel in range(1, self.channels):
            lag = _gcc_phat_lag(frame[:, channel], frame[:, 0], self.max_lag)
            if lag is None:
                return None
            lags.append(lag)
        self._history.append(np.asarray(lags, dtype=np.float64))
        # 峰值为 ch_i 相对 ch0 的滞后量；提前量取负号。
        return -np.median(np.stack(self._history), axis=0)


class Beamformer:
    """`ChannelDownmixer` 的波束策略实现（bytes 进、bytes 出）。"""

    def __init__(self, mode: str = "fixed", channels: int = 6, angle_deg: float = 0.0) -> None:
        if mode not in ("fixed", "doa"):
            raise ValueError("mode must be 'fixed' or 'doa'")
        self.channels = int(channels)
        self.mode = mode
        self._steering = steering_delays(angle_deg)
        self._delays = self._steering.copy()
        self.last_delays = self._delays.copy()
        self._das = DelayAndSum(self._delays, channels=self.channels)
        self._doa = GccPhatDoa(self.channels) if mode == "doa" else None

    def reset(self) -> None:
        self._das.reset()
        if self._doa is not None:
            self._doa.reset()

    def feed(self, pcm: bytes) -> bytes:
        frame_bytes = self.channels * 2
        usable = len(pcm) - (len(pcm) % frame_bytes)
        if usable <= 0:
            return b""
        samples = np.frombuffer(pcm[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        samples = samples.reshape(-1, self.channels)
        if self._doa is not None:
            estimate = self._doa.update(samples)
            # 失败/低信噪比回落固定指向；死区 + 每块限速避免逐帧抖动。
            target = estimate if estimate is not None else self._steering
            delta = target - self._delays
            delta = np.where(np.abs(delta) < DOA_DEADBAND, 0.0, delta)
            self._delays = self._delays + np.clip(delta, -DOA_STEP_LIMIT, DOA_STEP_LIMIT)
            self._das.set_delays(self._delays)
            self.last_delays = self._delays.copy()
        output = self._das.feed(samples)
        return np.clip(output * 32768.0, -32768, 32767).astype(np.int16).tobytes()
