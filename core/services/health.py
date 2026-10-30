"""健康快照组装（纯函数，不 import aiohttp / 设备 / 会话实现）。

`/api/health` 只做「取数 + 组装」：内存态字段总是返回；设备探针（mediaplayer
存活、内存、麦态）开销较大，由调用方按 `?probe=1` 决定是否采集。
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_health_payload", "livez_payload", "readyz_payload"]


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


def livez_payload() -> dict[str, Any]:
    """`/livez`：只证明进程与事件循环还在（不看设备）。"""

    return {"success": True, "data": {"status": "alive"}}


def readyz_payload(
    *, speaker_ready: bool, recording_expected: bool | None = None
) -> tuple[int, dict[str, Any]]:
    """`/readyz`：能否承接语音/设备命令，与 liveness 分离。

    - speaker 未就绪 ⇒ not_ready（没有设备连接，命令无处可发）
    - 用户主动「停止聆听」（recording_expected=False）⇒ not_ready（语音入口不可用）
    """

    reasons: list[str] = []
    if not speaker_ready:
        reasons.append("speaker_unavailable")
    if recording_expected is False:
        reasons.append("listening_stopped")
    ready = not reasons
    return (
        200 if ready else 503,
        {"success": ready,
         "data": {"status": "ready" if ready else "not_ready", "reasons": reasons}},
    )
