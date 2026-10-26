"""播放时长/完成等待的纯函数原语层（B-29 步 1，评审 S4）。

定位：本地 PCM 的「时长」是确定性算术，不是「播放完成」判据——Rust PCM
通道没有完成事件（router 通道返回即完成，buffer 播放连返回都不等），事件面
三源都观测不到它。因此完成等待下沉为纯函数原语（算术睡眠），而非接进
PlaybackEventAdapter；真机证实 buffer 播放可产生完成事件前，事件面不背这个锅。

本模块零依赖（纯 stdlib），可表驱动单测；调用方 = 对话循环提示音
（external_conversation / xiaozhi）与 speaker 的文本预算估算。
"""

import asyncio


def pcm_duration_seconds(
    pcm: bytes, *, sample_rate: int = 24000, sample_width: int = 2
) -> float:
    """int16 PCM 缓冲的播放时长（秒）。

    空 buffer / 字节数不足一个采样帧时返回 0.0（不算错误）；
    奇数字节按整帧向下取（截尾的字节不构成完整采样）。
    """
    if not pcm:
        return 0.0
    if sample_rate <= 0 or sample_width <= 0:
        return 0.0
    frame_bytes = sample_width  # 单声道 int16：一帧 = 一个采样
    frames = len(pcm) // frame_bytes
    return frames / float(sample_rate)


async def sleep_playback_done(pcm: bytes, *, sample_rate: int = 24000) -> None:
    """按 PCM 时长等待本地播放完成（算术睡眠，buffer 播放无完成事件）。

    取代散落三处的 `len(pcm) / (24000 * 2)` 手写算术；空 buffer 直接返回。
    """
    duration = pcm_duration_seconds(pcm, sample_rate=sample_rate)
    if duration > 0:
        await asyncio.sleep(duration)


# ---- 文本播放预算估算（自 speaker.py 迁入，speaker re-export 保持兼容）----

# 超时上限按文本估算（中文约 4.5 字/秒 + 6s 余量），与 api_server 的闸门估算同口径。
_PLAYBACK_MIN_BUDGET_MS = 8_000
_PLAYBACK_MAX_BUDGET_MS = 120_000


def estimate_playback_budget_ms(text: str | None, timeout_ms: float | None) -> float:
    """按文本估算播放完成等待上限，并以调用方 timeout 为硬上限。"""
    length = len((text or "").strip())
    estimate = min(
        _PLAYBACK_MAX_BUDGET_MS,
        max(_PLAYBACK_MIN_BUDGET_MS, length / 4.5 * 1000 + 6_000),
    )
    if timeout_ms is None:
        return estimate
    return max(1.0, min(float(timeout_ms), estimate))
