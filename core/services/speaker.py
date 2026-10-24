import asyncio
import os
from typing import Literal

import open_xiaoai_server

from core.device import CommandResult, get_device as get_capabilities
from core.ref import get_xiaoai, set_speaker
from core.utils.logger import logger

__all__ = ["CommandResult", "SpeakerManager"]


class SpeakerManager:
    """音箱硬件控制外观。

    所有设备命令都经 `core.device` 能力层发出（ticket 02）；
    本类只保留对外方法签名与既有行为，判据缺陷留给 ticket 03。
    """

    status: Literal["playing", "paused", "idle"] = "idle"

    def __init__(self):
        self.device = get_capabilities()
        set_speaker(self)

    async def get_playing(self, sync=False):
        """获取播放状态"""
        if sync:
            # 同步远端最新状态
            res = await self.device.playback_status()
            if "1" in res.stdout:
                self.status = "playing"
            elif "2" in res.stdout:
                self.status = "paused"
        return self.status

    async def set_playing(self, playing=True):
        """播放/暂停"""
        if playing:
            res = await self.device.playback_resume()
        else:
            res = await self.device.pause()
        return '"code": 0' in res.stdout

    async def play(
        self,
        text=None,
        url=None,
        buffer=None,
        server_file=None,
        blocking=True,
        timeout=10 * 60 * 1000,
    ):
        """
        播放文字、音频链接、音频流

        参数:
            text: 文字内容
            url: 音频链接
            buffer: 音频流
            server_file: 服务端本地音频文件路径
            timeout: 超时时长（毫秒），默认10分钟
            blocking: 是否阻塞运行
        """
        if server_file is not None:
            return await self.play_server_file(
                file_path=server_file,
                blocking=blocking,
            )

        if buffer is not None:
            return get_xiaoai().on_output_data(buffer)

        if blocking:
            if url:
                res = await self.device.play_url_blocking(url, timeout_ms=timeout)
            else:
                res = await self.device.play_text_blocking(text, timeout_ms=timeout)
            # 历史判据（阻塞 TTS 脚本成功时 rc 也可能是 1）：ticket 03 修。
            return res.request.exit_code == 0

        if url:
            res = await self.device.play_url(url, timeout_ms=timeout)
        else:
            res = await self.device.tts_speak(text, timeout_ms=timeout)
        return '"code": 0' in res.request.stdout

    async def play_server_file(
        self,
        file_path: str,
        blocking: bool = True,
        sample_rate: int = 24000,
    ) -> bool:
        """播放服务端本地音频文件（解码为 PCM 后推流到音箱）"""
        if not file_path:
            raise ValueError("file_path is required")

        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)

        logger.info(
            f"[Speaker] Playing local file via Rust audio pipeline: {file_path}, "
            f"sample_rate={sample_rate}"
        )

        if blocking:
            await open_xiaoai_server.play_audio_file(file_path, sample_rate=sample_rate)
            return True

        asyncio.create_task(
            open_xiaoai_server.play_audio_file(file_path, sample_rate=sample_rate)
        )
        return True

    async def stop_device_audio(self) -> None:
        """
        停止设备上的全部播放链路。
        aplay 不立即重启，由 Rust 侧 ensure_player_ready() 在首次
        发送音频数据时按需启动，避免空 buffer 导致 underrun。
        """
        await self.device.stop_audio()
        await open_xiaoai_server.stop_playing()

    async def stop_local_playback(self) -> None:
        """只停设备侧阻塞/非阻塞播报进程（不含 PCM 通道）。"""
        await self.device.stop_audio()

    async def wake_up(self, awake=True, silent=True):
        """
        （取消）唤醒小爱

        参数:
            awake: 是否唤醒
            silent: 是否静默唤醒
        """
        if awake:
            res = await self.device.wake(silent=silent)
        else:
            res = await self.device.sleep()
        return '"code": 0' in res.stdout

    async def ask_xiaoai(self, text: str, silent=False):
        """
        把文字指令交给原来的小爱执行

        参数:
            text: 文字指令
            silent: 是否静默执行
        """
        res = await self.device.ask_xiaoai(text, silent=silent)
        return '"code": 0' in res.stdout

    async def abort_xiaoai(self):
        """
        中断原来小爱的运行

        注意：重启需要大约 1-2s 的时间，在此期间无法使用小爱音箱自带的 TTS 服务
        """
        res = await self.device.restart_xiaoai_service()
        return res.request is not None and res.request.exit_code == 0

    async def get_boot(self):
        """获取启动分区"""
        res = await self.device.boot_partition_get()
        return res.stdout.strip()

    async def set_boot(self, boot_part: Literal["boot0", "boot1"]):
        """设置启动分区"""
        res = await self.device.boot_partition_set(boot_part)
        return boot_part in res.stdout

    async def get_device(self):
        """获取设备型号、序列号信息"""
        res = await self.device.device_info()
        info = res.stdout.strip().split(" ")
        return {
            "model": info[0] if len(info) > 0 else "unknown",
            "sn": info[1] if len(info) > 1 else "unknown",
        }

    async def get_mic(self):
        """获取麦克风状态"""
        res = await self.device.mic_get()
        return "on" if "on" in res.stdout else "off"

    async def set_mic(self, on=True):
        """打开/关闭麦克风"""
        # 历史语义（见 AGENTS.md「set_mic 语义反转」，ticket 03 修）：
        # bridge 的 on=True 实际发出 event:7（设备侧闭麦），on=False 发 event:8。
        # 能力层按设备真值命名（muted=True ⇒ event:7），此处保持原映射与返回值。
        res = await self.device.mic_set(muted=bool(on))
        return '"code":0' in res.stdout

    async def clear_mic_mute_flag(self):
        """清掉设备侧 /tmp/mipns/mute 标志（停止聆听的恢复兜底）。"""
        return await self.device.mic_clear_mute_flag()

    async def run_shell(self, script: str, timeout=10000):
        """
        执行脚本

        参数:
            script: 脚本内容
            timeout: 超时时间（毫秒）
        """
        res = await self.device.shell(script, timeout_ms=timeout)
        return res.request
