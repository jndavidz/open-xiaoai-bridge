import asyncio
import os
from typing import Literal

import open_xiaoai_server

from core.device import (
    TTS_TEMP_PREFIX,
    CommandResult,
    get_device as get_capabilities,
)
from core.ref import get_xiaoai, set_speaker
from core.utils.logger import logger

__all__ = ["CommandResult", "SpeakerManager"]

# ---- 阻塞播报的判据与轮询参数 ----

# `player_get_context.info.status`：1 = 播放中；0/2 = 链路空闲（0 空闲、2 暂停）。
_PLAYBACK_PLAYING_STATUS = 1
_PLAYBACK_IDLE_STATUSES = (0, 2)

# 轮询间隔与「等播放真正开始」的宽限：短音频可能整段落在第一个轮询间隔内，
# 宽限期内始终只看到空闲就按「已播完」处理（事件驱动留给 ticket 05）。
_PLAYBACK_POLL_INTERVAL_MS = 200
_PLAYBACK_START_GRACE_MS = 1000

# 超时上限按文本估算（中文约 4.5 字/秒 + 6s 余量），与 api_server 的闸门估算同口径。
_PLAYBACK_MIN_BUDGET_MS = 8_000
_PLAYBACK_MAX_BUDGET_MS = 120_000

# 合成超时：真机实测 < 1s/段，留足余量但不占用播放预算。
_TTS_SYNTHESIS_TIMEOUT_MS = 6_000


def estimate_playback_budget_ms(text: str, timeout_ms: float | None) -> float:
    """按文本估算播放完成等待上限，并以调用方 timeout 为硬上限。"""

    length = len((text or "").strip())
    estimate = min(
        _PLAYBACK_MAX_BUDGET_MS,
        max(_PLAYBACK_MIN_BUDGET_MS, length / 4.5 * 1000 + 6_000),
    )
    if timeout_ms is None:
        return estimate
    return max(1.0, min(float(timeout_ms), estimate))


class SpeakerManager:
    """音箱硬件控制外观。

    所有设备命令都经 `core.device` 能力层发出（ticket 02），本类只做多步编排
    与对外方法签名；判据（合成落盘 / 回读 / 音量 / 麦语义）按设备真值（ticket 03）。
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
                # 阻塞 URL 播放器的退出码是可靠判据（与设备 TTS 脚本不同）。
                return res.request is not None and res.request.exit_code == 0
            return await self._play_text_blocking(text, timeout)

        if url:
            res = await self.device.play_url(url, timeout_ms=timeout)
        else:
            res = await self.device.tts_speak(text, timeout_ms=timeout)
        return '"code": 0' in res.stdout

    async def _play_text_blocking(self, text, timeout_ms) -> bool:
        """阻塞播报：合成落盘 → 设备本地播放 → 回读空闲 → 清理临时文件。

        成功判据 = 合成成功 + 播放命令被接收 + 回读空闲（或超时兜底）；
        不再使用设备脚本退出码（设备 TTS 脚本成功时 rc 也是 1）。
        合成失败才回退到设备脚本，回退路径由能力层做「先 JSON、后 shell」双层转义。
        """
        content = text or "你好"

        synth = await self.device.tts_synthesize(
            content, timeout_ms=_TTS_SYNTHESIS_TIMEOUT_MS
        )
        path = str((synth.readback or {}).get("path") or "")
        if not synth.ok or not path:
            logger.warning(
                f"[Speaker] TTS synthesis failed ({synth.error or 'no_path'}), "
                "falling back to device script"
            )
            return await self._play_text_via_script(content, timeout_ms)

        play = await self.device.play_url(f"file://{path}")
        if not play.ok:
            logger.warning(
                f"[Speaker] player_play_url rejected file://{path}: {play.error}"
            )
            await self._cleanup_tts_file(path)
            return False

        try:
            return await self._wait_playback_done(content, timeout_ms)
        finally:
            await self._cleanup_tts_file(path)

    async def _play_text_via_script(self, text, timeout_ms) -> bool:
        """兜底路径：设备阻塞 TTS 脚本。判据仍是回读，不是退出码。"""

        res = await self.device.play_text_blocking(text, timeout_ms=timeout_ms)
        if res.exception is not None or res.request is None:
            logger.warning(
                f"[Speaker] device script fallback failed: {res.error or res.exception}"
            )
            return False
        # 该脚本成功也返 1 ⇒ 不看 rc，改为等播放上下文回到空闲。
        return await self._wait_playback_done(text, timeout_ms)

    async def _wait_playback_done(self, text, timeout_ms) -> bool:
        """轮询播放上下文，直到空闲（0/2）或超时；读不到状态判失败。"""

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        budget_ms = estimate_playback_budget_ms(text, timeout_ms)
        grace_ms = min(budget_ms, _PLAYBACK_START_GRACE_MS)
        deadline = started_at + budget_ms / 1000.0
        started = False
        readback_ok = False

        while True:
            ctx = await self.device.player_context()
            status = (ctx.readback or {}).get("status") if ctx.readback else None
            if ctx.ok and isinstance(status, int) and not isinstance(status, bool):
                readback_ok = True
                if status == _PLAYBACK_PLAYING_STATUS:
                    started = True
                elif status in _PLAYBACK_IDLE_STATUSES:
                    if started:
                        return True
                    # 宽限期内始终空闲：短音频已播完（或根本没开始，设备侧无法区分）。
                    if (loop.time() - started_at) * 1000 >= grace_ms:
                        logger.debug(
                            f"[Speaker] playback already idle (status={status}), "
                            f"grace={grace_ms:.0f}ms"
                        )
                        return True

            now = loop.time()
            if now >= deadline:
                if not readback_ok:
                    logger.warning(
                        "[Speaker] playback status readback failed, cannot confirm "
                        f"completion (budget={budget_ms:.0f}ms)"
                    )
                    return False
                logger.warning(
                    f"[Speaker] playback completion not observed, timeout fallback "
                    f"(budget={budget_ms:.0f}ms, started={started})"
                )
                return True

            await asyncio.sleep(
                min(_PLAYBACK_POLL_INTERVAL_MS / 1000.0, max(deadline - now, 0.01))
            )

    async def _cleanup_tts_file(self, path: str) -> None:
        """删除合成临时文件；前缀不是 /tmp/tts/ 时只告警、绝不删除。"""

        if not path.startswith(TTS_TEMP_PREFIX):
            logger.warning(f"[Speaker] skip cleanup for non-tts path: {path!r}")
            return
        try:
            await self.device.tts_cleanup(path)
        except Exception as e:  # noqa: BLE001 - 清理失败不影响播报结论
            logger.warning(f"[Speaker] cleanup failed for {path}: {e}")

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
        """打开/关闭麦克风（on=True ⇒ 开麦）。

        设备真值：`event:7` 创建 /tmp/mipns/mute（闭麦），`event:8` 删除（开麦）。
        `event:8` 在麦已开时返回 `code:-1` 属幂等语义 ⇒ 成败以回读为准。
        """
        res = await self.device.mic_set(muted=not on)
        return bool(res.ok)

    async def set_volume(self, level: int) -> bool:
        """设置音量（百分比 0-100），回读一致才算成功。"""
        res = await self.device.volume_set(int(level))
        if not res.ok:
            logger.warning(f"[Speaker] set_volume({level}) failed: {res.error}")
        return bool(res.ok)

    async def get_volume(self) -> int:
        """读取音量（player_get_context 回读，百分比口径）；读不到返回 -1。"""
        res = await self.device.volume_get()
        volume = (res.readback or {}).get("volume") if res.readback else None
        if not res.ok or not isinstance(volume, int) or isinstance(volume, bool):
            logger.warning(f"[Speaker] get_volume failed: {res.error or 'volume_missing'}")
            return -1
        return volume

    async def mediaplayer_alive(self) -> bool | None:
        """mediaplayer 是否被 procd 托管且 running（读不到返回 None，不抛异常）。"""
        try:
            res = await self.device.service_status("mediaplayer")
        except Exception as e:  # noqa: BLE001 - 健康探针不得抛
            logger.warning(f"[Speaker] mediaplayer liveness probe failed: {e}")
            return None
        running = (res.readback or {}).get("running") if res.readback else None
        return bool(running) if isinstance(running, bool) else None

    async def memory_free(self) -> int | None:
        """系统剩余内存（字节，`system info` 的 memory.free）；读不到返回 None。"""
        try:
            res = await self.device.system_memory()
        except Exception as e:  # noqa: BLE001 - 健康探针不得抛
            logger.warning(f"[Speaker] memory probe failed: {e}")
            return None
        free = (res.readback or {}).get("free") if res.readback else None
        if isinstance(free, bool) or not isinstance(free, (int, float)):
            return None
        return int(free)

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
