"""设备后端：把托管播放接到既有能力层（B-06）。

两条通道各自声明能力，不互相顶替：

  - **DLNA（主）**：`player_play_url` 投送 URI；停止走 `stop_audio`（显式 Stop，
    不等自然 STOPPED）；暂停走设备播放控制 CLI；位置/seek/resume 未验证。
  - **RPC（降级）**：同一 `player_play_url` 通道 + `playback_resume`（设备 CLI 的 play）；
    位置/seek 同样未验证。

两者都不复用旧 `/api/play/url` 的整首 announce 闸门——托管路径的闸门语义由
ManagedService/播放编排负责。
"""

from __future__ import annotations

from core.music.player import DLNA, RPC, BackendProfile

_CONTEXT_STATE = {1: "playing", 2: "paused", 0: "idle"}


class DeviceBackend:
    profile: BackendProfile = DLNA

    def __init__(self, device=None) -> None:
        self._device = device

    @property
    def device(self):
        if self._device is None:
            from core.device import get_device

            self._device = get_device()
        return self._device

    async def play(self, uri: str) -> bool:
        res = await self.device.play_url(uri)
        return bool(getattr(res, "ok", False))

    async def stop(self) -> bool:
        res = await self.device.stop_audio()
        return bool(getattr(res, "ok", False))

    async def pause(self) -> bool:
        res = await self.device.pause()
        return bool(getattr(res, "ok", False))

    async def resume(self) -> bool:
        res = await self.device.playback_resume()
        return bool(getattr(res, "ok", False))

    async def state(self) -> str:
        res = await self.device.player_context()
        readback = getattr(res, "readback", None) or {}
        status = readback.get("status")
        return _CONTEXT_STATE.get(status, "unknown")

    async def position(self) -> float | None:
        return None   # 设备没有已验证的音频位置回读


class DlnaBackend(DeviceBackend):
    profile = DLNA


class RpcBackend(DeviceBackend):
    profile = RPC
