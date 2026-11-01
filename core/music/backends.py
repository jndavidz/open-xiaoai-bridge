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

from core.music.pcm_orchestrator import PcmOrchestrator
from core.music.player import DLNA, PCM, RPC, BackendProfile

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


class PcmBackend:
    """PCM 推流 backend（pcm-backend spec 票 03）：桥侧记账回答一切。

    与 DeviceBackend 的根本差异：**不依赖设备 status/mediaplayer**——
    state()/position() 由 PcmOrchestrator 的推流会话记账回答（URL 投送会话
    status 恒 3 的盲判问题在 PCM 会话下从根上不存在）；pause/resume 由
    编排器驱动（stop/记忆偏移/start_ms 续推），不碰设备播放控制。
    seek = 停推 + 从目标偏移重投（桥侧重投）。
    """

    profile: BackendProfile = PCM

    def __init__(self, orchestrator: PcmOrchestrator | None = None) -> None:
        self.orch = orchestrator or PcmOrchestrator()

    async def play(self, uri: str) -> bool:
        try:
            await self.orch.start(uri)
            return True
        except Exception as exc:  # noqa: BLE001 - 起播失败如实上报为 False
            from core.utils.logger import logger

            logger.warning(f"[PcmBackend] play rejected: {type(exc).__name__}: {exc}")
            return False

    async def stop(self) -> bool:
        await self.orch.stop()
        return True

    async def pause(self) -> bool:
        await self.orch.pause()
        return True

    async def resume(self) -> bool:
        await self.orch.resume()
        return True

    async def state(self) -> str:
        self.orch.refresh()
        return self.orch.state()

    async def position(self) -> float | None:
        # 桥侧记账（含推流终态后的冻结曲末值，由编排器持有）
        self.orch.refresh()
        pos_ms = self.orch.position_ms()
        return None if pos_ms is None else pos_ms / 1000.0

    async def seek(self, position_s: float) -> None:
        """桥侧重投：停推 + 从目标偏移 start_ms 重推（无设备 seek 依赖）。"""

        await self.orch.seek_to(int(position_s * 1000))

    def duration_s(self) -> float | None:
        """native 流头解析出的时长（无索引格式为 None，调用方回落 resolver 值）。"""

        duration_ms = self.orch.duration_ms()
        return None if duration_ms is None else duration_ms / 1000.0
