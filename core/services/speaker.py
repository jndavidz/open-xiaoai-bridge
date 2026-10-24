import asyncio
import os
from contextlib import asynccontextmanager
from typing import Literal

import open_xiaoai_server

from core.device import (
    TTS_TEMP_PREFIX,
    CommandResult,
    get_device as get_capabilities,
    get_playback_events,
)
from core.ref import get_kws, get_xiaoai, set_speaker
from core.utils.logger import logger

__all__ = ["CommandResult", "SpeakerManager"]

# ---- 阻塞播报的判据参数 ----

# 轮询间隔与「等播放真正开始」的宽限改由 `event_plane` 配置拥有
# （默认 200ms / 1000ms），见 core/device/playback_events.py。

# 超时上限按文本估算（中文约 4.5 字/秒 + 6s 余量），与 api_server 的闸门估算同口径。
_PLAYBACK_MIN_BUDGET_MS = 8_000
_PLAYBACK_MAX_BUDGET_MS = 120_000

# 合成超时：真机实测 < 1s/段，留足余量但不占用播放预算。
_TTS_SYNTHESIS_TIMEOUT_MS = 6_000

# 麦克风闸门默认持有的上限（超过即告警并强制恢复快照）。
_MIC_GATE_MAX_SECONDS = 300.0


class _MicGateHold:
    """一次 `mic_gate` 的持有状态（watchdog 与退出路径共享）。"""

    __slots__ = ("restored", "timed_out")

    def __init__(self) -> None:
        self.restored = False
        self.timed_out = False


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
        # 播报串行化：mic_gate 持有期间独占，避免并发播报嵌套导致
        # 「A 恢复 on、B 恢复 off」把设备永久留在静音态。
        self._mic_gate_lock = asyncio.Lock()
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
        # 该脚本成功也返 1 ⇒ 不能用 rc==0 判成功；但 rc 既不是 0/1、
        # 或出现已知 JSON 解析错误时，是明确的失败，不能按成功算。
        if res.request.exit_code not in (0, 1) or "Failed to parse" in (
            res.stderr or ""
        ):
            logger.warning(
                f"[Speaker] device script fallback rejected "
                f"(rc={res.request.exit_code}, stderr={res.stderr.strip()!r})"
            )
            return False
        return await self._wait_playback_done(text, timeout_ms)

    async def _wait_playback_done(self, text, timeout_ms) -> bool:
        """等待播放完成（事件适配器，ticket 05），保留超时兜底。

        语义与 ticket 03 一致：
          - 观察到 playing 后转空闲 ⇒ 已播完；
          - 从未观察到 playing 但已空闲 ⇒ 1s 宽限后按「短音频已播完」处理；
          - 只观察到 playing、没有结束 ⇒ 超时兜底，仍算完成（不是失败）；
          - 整个等待期间读不到状态 ⇒ 判失败，不能确认完成。
        """

        budget_ms = estimate_playback_budget_ms(text, timeout_ms)
        finished, started, reason = await get_playback_events().wait_for_finish(
            budget_ms
        )
        if not finished:
            logger.warning(
                "[Speaker] playback status readback failed, cannot confirm "
                f"completion (budget={budget_ms:.0f}ms, reason={reason})"
            )
        elif reason == "timeout":
            logger.warning(
                f"[Speaker] playback completion not observed, timeout fallback "
                f"(budget={budget_ms:.0f}ms, started={started})"
            )
        return finished

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

    # ------------------------------------------------------------------
    # 麦克风闸门：播报期间固件级闭麦 + 退出时恢复进入前快照
    #
    # 设备真值（pnshelper-event-space.md §3.1）：`event:7` 创建 /tmp/mipns/mute
    # ⇒ 固件不再唤醒（与 bridge 自己的 ALSA 采集/KWS 并存）；固件无自愈，
    # 所以闸门必须 try/finally + 上限看门狗，绝不能无条件开麦
    #（否则会把用户「停止聆听」的设备级静音打开）。
    # 与 KWS 播放闸门（gate_on/gate_off，bridge 不喂帧）叠加，互不替代。
    # ------------------------------------------------------------------

    async def _read_mic_state(self) -> str | None:
        """读麦态快照：`"on"` / `"off"`；读取失败返回 `None`。

        与 `get_mic()` 走同一条 `mic_get` 通道，但把「读不到」与「off」区分开，
        供闸门的 fallback 判定使用（`get_mic()` 的对外语义保持不变）。
        """
        try:
            res = await self.device.mic_get()
        except Exception as e:  # noqa: BLE001 - 读不到只能降级
            logger.warning(f"[Speaker] mic snapshot read failed: {e}")
            return None
        if res.exception is not None or res.request is None:
            logger.warning(
                f"[Speaker] mic snapshot unreadable: {res.error or res.exception}"
            )
            return None
        out = (res.stdout or "").strip()
        if "on" in out:
            return "on"
        if "off" in out:
            return "off"
        logger.warning(f"[Speaker] mic snapshot unexpected output: {out!r}")
        return None

    @staticmethod
    def _kws_listen_disabled() -> bool:
        """KWS 是否处于「停止聆听」（T7.6 持久开关）。KWS 未加载 ⇒ False。"""
        try:
            kws = get_kws()
        except Exception:  # noqa: BLE001 - 引用表异常不应影响播报
            return False
        if kws is None:
            return False
        try:
            checker = getattr(kws, "is_listening_disabled", None)
            if callable(checker):
                return bool(checker())
        except Exception:  # noqa: BLE001 - KWS 未 start() 时属性可能不存在
            return False
        return bool(getattr(kws, "listen_disabled", False))

    async def _restore_mic_state(self, snapshot: str | None, reason: str) -> bool:
        """恢复进入闸门前的麦态快照（on→on、off→off）。

        快照读不到时按 KWS「停止聆听」推断：未停止聆听 ⇒ 恢复开麦，否则保持 off。
        绝不做无条件开麦。
        """
        if snapshot == "on":
            target_on = True
        elif snapshot == "off":
            target_on = False
        else:
            target_on = not self._kws_listen_disabled()
            logger.warning(
                f"[Speaker] mic gate has no snapshot (reason={reason!r}), "
                f"fallback by kws_listen_disabled -> {'on' if target_on else 'off'}"
            )
        try:
            ok = await self.set_mic(on=target_on)
        except Exception as e:  # noqa: BLE001 - finally 里不得抛
            logger.warning(f"[Speaker] mic gate restore failed (reason={reason!r}): {e}")
            return False
        target = "on" if target_on else "off"
        if not ok:
            logger.warning(
                f"[Speaker] mic gate restore not confirmed "
                f"(reason={reason!r}, target={target}, snapshot={snapshot})"
            )
        return bool(ok)

    async def _mic_gate_watchdog(
        self,
        hold: _MicGateHold,
        snapshot: str | None,
        reason: str,
        max_seconds: float,
    ) -> None:
        """持有超上限：告警并强制恢复快照（固件无自愈，防永久失聪）。"""
        await asyncio.sleep(max_seconds)
        hold.timed_out = True
        logger.warning(
            f"[Speaker] mic gate held over {max_seconds:g}s "
            f"(reason={reason!r}) — forcing mic restore, caller may have leaked the gate"
        )
        hold.restored = await self._restore_mic_state(snapshot, reason)

    @asynccontextmanager
    async def mic_gate(
        self,
        reason: str = "playback",
        max_seconds: float = _MIC_GATE_MAX_SECONDS,
    ):
        """播报期间设备级闭麦，退出时恢复进入前快照。

        - 进入：读麦态快照 → `set_mic(False)`（设备级闭麦）；
          回读失败只告警，不阻断播报。
        - 退出（finally）：恢复快照（on→on、off→off），**绝不无条件开麦**；
          快照读不到时按 KWS `listen_disabled` 推断。
        - 持有超过 `max_seconds`：告警并强制恢复快照。
        - **串行**：闸门持有期间独占；并发播报会排队，避免嵌套快照互相覆盖。
        """
        async with self._mic_gate_lock:
            snapshot = await self._read_mic_state()
            if snapshot is None:
                logger.warning(
                    f"[Speaker] mic gate opened without snapshot (reason={reason!r})"
                )

            muted = False
            try:
                muted = await self.set_mic(on=False)
            except Exception as e:  # noqa: BLE001 - 闭麦失败不阻断播报
                logger.warning(f"[Speaker] mic gate mute failed (reason={reason!r}): {e}")
            if not muted:
                logger.warning(
                    f"[Speaker] mic gate mute not confirmed (reason={reason!r}, "
                    f"snapshot={snapshot})"
                )

            hold = _MicGateHold()
            watchdog = asyncio.create_task(
                self._mic_gate_watchdog(hold, snapshot, reason, max_seconds)
            )
            try:
                yield
            finally:
                if not hold.restored:
                    hold.restored = await self._restore_mic_state(snapshot, reason)
                watchdog.cancel()
                try:
                    await watchdog
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # noqa: BLE001 - 看门狗异常不影响退出
                    logger.warning(f"[Speaker] mic gate watchdog failed: {e}")

    async def reconcile_mic_state(self) -> bool:
        """启动期麦态自愈：清掉 bridge 崩溃遗留的设备级闭麦。

        固件无自愈，崩溃可能把 `/tmp/mipns/mute` 留在设备上（设备彻底听不见）。
        仅当麦为 off 且 KWS 未处于「停止聆听」时恢复开麦；
        用户显式停止聆听时保持 off。返回是否执行了恢复。
        """
        try:
            state = await self._read_mic_state()
        except Exception as e:  # noqa: BLE001 - 自愈不得阻断启动
            logger.warning(f"[Speaker] mic reconcile read failed: {e}")
            return False
        if state != "off":
            return False
        if self._kws_listen_disabled():
            logger.info(
                "[Speaker] mic is off but KWS listening is disabled, keep muted"
            )
            return False
        ok = await self._restore_mic_state("on", "startup reconcile")
        logger.warning(
            f"[Speaker] stale device mic mute detected (bridge crash?), restore on: ok={ok}"
        )
        return ok

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
