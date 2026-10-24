"""多声道（LX06 六麦）PCM 入口处理：帧对齐、选麦、下混。

设计约束：
- **纯逻辑 + 仅标准库**：本模块不 import numpy，便于用系统 Python 跑单测；
  VAD/KWS/ASR 的对外接口保持不变（仍只吃 1ch int16 PCM）。
- 设备侧 `arecord -c 6 -f S16_LE -r 16000` 给出的是**交错**的 6 路样本；
  直接 `np.frombuffer` 会把它当成连续单声道时间序列 ⇒ 必须先按 `2*channels`
  字节切帧再降声道。
- 降声道策略默认「滑动窗口内最强单麦 + 迟滞」，避免逐帧切换造成爆音；
  可切等权平均（average）或固定声道（fixed）。

数据形状约定：所有 `feed()` 输入/输出都是小端 int16 交错 PCM 字节。
"""

from __future__ import annotations

import array
import collections
import sys

BYTES_PER_SAMPLE = 2

STRATEGY_STRONGEST = "strongest"
STRATEGY_AVERAGE = "average"
STRATEGY_FIXED = "fixed"
VALID_STRATEGIES = (STRATEGY_STRONGEST, STRATEGY_AVERAGE, STRATEGY_FIXED)


def normalize_strategy(value, default: str = STRATEGY_STRONGEST) -> str:
    """把配置值归一化为合法策略名（非法值回落默认）。"""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in VALID_STRATEGIES:
            return candidate
    return default


def _to_samples(pcm: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _to_pcm(samples: array.array) -> bytes:
    if sys.byteorder != "little":
        samples = array.array("h", samples)
        samples.byteswap()
    return samples.tobytes()


def _round_div(total: int, count: int) -> int:
    """四舍五入的整数除法（正负都对称，避免 Python 向下取整偏置）。"""
    if total >= 0:
        return (total + count // 2) // count
    return -((-total + count // 2) // count)


def frame_rms(frame: bytes, channels: int) -> list[float]:
    """计算一帧交错 PCM 每声道的 RMS（int16 量纲）。"""
    channels = max(1, int(channels))
    samples = _to_samples(frame)
    usable = len(samples) - (len(samples) % channels)
    count = usable // channels
    if count <= 0:
        return [0.0] * channels
    sums = [0] * channels
    for index in range(usable):
        sample = samples[index]
        sums[index % channels] += sample * sample
    return [(value / count) ** 0.5 for value in sums]


class FrameAligner:
    """按整样本帧（channels 个 int16）对齐，跨帧保留残留字节。

    `feed()` 只返回「完整声道帧」前缀，剩余字节留在内部缓冲，下一次
    `feed()` 拼接后继续输出 ⇒ 不丢样本、不重复样本。`flush()` 返回残留。
    """

    def __init__(self, channels: int = 6) -> None:
        if int(channels) < 1:
            raise ValueError("channels must be >= 1")
        self.channels = int(channels)
        self.frame_bytes = self.channels * BYTES_PER_SAMPLE
        self._residual = bytearray()

    @property
    def residual_bytes(self) -> int:
        return len(self._residual)

    def reset(self) -> None:
        self._residual.clear()

    def feed(self, data: bytes) -> bytes:
        if not data:
            return b""
        self._residual.extend(data)
        whole = len(self._residual) - (len(self._residual) % self.frame_bytes)
        if whole <= 0:
            return b""
        aligned = bytes(self._residual[:whole])
        del self._residual[:whole]
        return aligned

    def flush(self) -> bytes:
        leftover = bytes(self._residual)
        self._residual.clear()
        return leftover


class ChannelSelector:
    """滑动窗口 RMS 选麦 + 迟滞。

    - 每帧（一次 `feed()` 的对齐数据）计算各声道 RMS，窗口内取均值；
    - 首帧直接选最响声道；
    - 之后只有「候选声道均值 > 当前声道均值 × hysteresis_ratio」且
      「连续 min_hold_frames 帧都是它最响」才切换，避免逐帧抖动。
    """

    def __init__(
        self,
        channels: int = 6,
        window_frames: int = 5,
        hysteresis_ratio: float = 1.25,
        min_hold_frames: int = 3,
        silence_floor: float = 1.0,
    ) -> None:
        self.channels = max(1, int(channels))
        self.window_frames = max(1, int(window_frames))
        self.hysteresis_ratio = max(1.0, float(hysteresis_ratio))
        self.min_hold_frames = max(1, int(min_hold_frames))
        self.silence_floor = max(0.0, float(silence_floor))
        self._history: collections.deque[list[float]] = collections.deque(
            maxlen=self.window_frames
        )
        self.current = 0
        self._initialized = False
        self._best_streak = 0

    def reset(self) -> None:
        self._history.clear()
        self.current = 0
        self._initialized = False
        self._best_streak = 0

    def _smoothed_levels(self) -> list[float]:
        levels = [0.0] * self.channels
        for frame_levels in self._history:
            for channel, value in enumerate(frame_levels):
                levels[channel] += value
        count = max(1, len(self._history))
        return [value / count for value in levels]

    def update(self, frame: bytes) -> int:
        frame_levels = frame_rms(frame, self.channels)
        self._history.append(frame_levels)
        levels = self._smoothed_levels()
        best = max(range(self.channels), key=lambda channel: levels[channel])

        if not self._initialized:
            self.current = best
            self._initialized = True
            return self.current

        instant_best = max(
            range(self.channels), key=lambda channel: frame_levels[channel]
        )
        if best == self.current:
            self._best_streak = 0
            return self.current
        if instant_best != best:
            # 平滑窗口里的「最响」只是单帧尖峰残留，本帧已不是它 ⇒ 不累计切换
            self._best_streak = 0
            return self.current

        self._best_streak += 1
        current_level = levels[self.current]
        candidate_level = levels[best]
        if candidate_level <= self.silence_floor:
            # 全静音时不因比值抖动切换
            return self.current

        clearly_better = candidate_level > current_level * self.hysteresis_ratio
        if clearly_better and self._best_streak >= self.min_hold_frames:
            self.current = best
            self._best_streak = 0
        return self.current


class ChannelDownmixer:
    """六麦入口：帧对齐 → 选麦/平均 → 1ch int16 PCM。

    用法：`feed(chunk)` 可多次拼出同一批样本；返回可能是空串（残留未满帧）。
    `channels=1` 时等价于直通（仅做 2 字节对齐），与旧行为一致。
    """

    def __init__(
        self,
        channels: int = 6,
        strategy: str = STRATEGY_STRONGEST,
        fixed_channel: int = 0,
        window_frames: int = 5,
        hysteresis_ratio: float = 1.25,
        min_hold_frames: int = 3,
    ) -> None:
        self.channels = max(1, int(channels))
        self.strategy = normalize_strategy(strategy)
        self.fixed_channel = max(0, min(int(fixed_channel), self.channels - 1))
        self.aligner = FrameAligner(self.channels)
        self.selector = (
            ChannelSelector(
                channels=self.channels,
                window_frames=window_frames,
                hysteresis_ratio=hysteresis_ratio,
                min_hold_frames=min_hold_frames,
            )
            if self.strategy == STRATEGY_STRONGEST
            else None
        )
        self.frame_count = 0
        self.current_channel = self._initial_channel()

    def _initial_channel(self):
        if self.strategy == STRATEGY_FIXED:
            return self.fixed_channel
        if self.channels == 1 or self.strategy == STRATEGY_AVERAGE:
            return None
        return 0

    def reset(self) -> None:
        self.aligner.reset()
        if self.selector is not None:
            self.selector.reset()
        self.frame_count = 0
        self.current_channel = self._initial_channel()

    def feed(self, data: bytes) -> bytes:
        aligned = self.aligner.feed(data)
        if not aligned:
            return b""
        self.frame_count += 1
        if self.channels == 1:
            return aligned
        return self._downmix(aligned)

    def _downmix(self, aligned: bytes) -> bytes:
        if self.strategy == STRATEGY_AVERAGE:
            samples = _to_samples(aligned)
            averaged = array.array(
                "h",
                (
                    _round_div(sum(samples[i : i + self.channels]), self.channels)
                    for i in range(0, len(samples), self.channels)
                ),
            )
            return _to_pcm(averaged)

        if self.strategy == STRATEGY_FIXED:
            channel = self.fixed_channel
        else:
            channel = self.selector.update(aligned)
        self.current_channel = channel
        samples = _to_samples(aligned)
        return _to_pcm(samples[channel :: self.channels])
