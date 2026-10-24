"""健康快照组装（纯函数，不 import aiohttp / 设备 / 会话实现）。

`/api/health` 只做「取数 + 组装」：内存态字段总是返回；设备探针（mediaplayer
存活、内存、麦态）开销较大，由调用方按 `?probe=1` 决定是否采集。
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_health_payload"]


def build_health_payload(
    *,
    speaker_ready: bool,
    audio: dict[str, Any] | None = None,
    playback: dict[str, Any] | None = None,
    inject: dict[str, Any] | None = None,
    probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装 `/api/health` 的响应体。

    只暴露运行状态与计数，不包含凭证、完整文本或设备敏感信息。
    `probe` 为 None 表示本次未采集设备探针（未带 `?probe=1`）。
    """

    data: dict[str, Any] = {
        "status": "healthy",
        "speaker_ready": bool(speaker_ready),
        "audio": dict(audio or {}),
        "playback": dict(playback or {}),
        "inject": dict(inject or {}),
    }
    if probe is not None:
        data["probe"] = dict(probe)
    return {"success": True, "data": data}
