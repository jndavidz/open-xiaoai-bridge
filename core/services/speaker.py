import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Literal

import open_xiaoai_server

from core.device import (
    TTS_TEMP_PREFIX,
    CommandResult,
    get_device as get_capabilities,
    get_playback_events,
)
from core.ref import get_kws, get_xiaoai, set_speaker
from core.utils.logger import logger
from core.utils import turn_trace

__all__ = [
    "AnnounceRejected",
    "CommandResult",
    "SpeakerManager",
    "estimate_playback_budget_ms",
    "find_blocked_word",
]

# ---- 阻塞播报的判据参数 ----

# 轮询间隔与「等播放真正开始」的宽限改由 `event_plane` 配置拥有
# （默认 200ms / 1000ms），见 core/device/playback_events.py。

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
    """按文本估算播放完成等待上限，并以调用方 timeout 为硬上限。

    B-29 步 1：实现迁至 core/services/audio/playback_timing.py（对话域共用），
    此处保留 re-export——既有调用方（announce/mic_gate/api_server）与测试
    （test_judgement_fixes 经模块属性引用）不受影响。
    """
    from core.services.audio.playback_timing import estimate_playback_budget_ms as _impl

    return _impl(text, timeout_ms)


class AnnounceRejected(RuntimeError):
    """词表安全检查拒绝：播报文案命中唤醒/指令词表（spec C2，默认拒绝）。

    命中的词在 `word` 上（日志与 HTTP 响应只给词本身，不给全文，
    与 api_server 的 `_redact_text` 同口径）。
    """

    def __init__(self, word: str):
        super().__init__(f"播报文案命中唤醒/指令词表：{word!r}，已拒绝播报")
        self.word = word


def _kws_keywords() -> list[str]:
    """词源 1：KWS 总词表（wakeup.keywords，含面板运行时覆盖层）。

    读活配置（每次检查现取），config 热重载后下一个播报即用新词表。
    配置不可用时按空处理并告警——检查缺席绝不能变成崩溃，但必须有痕。
    """
    try:
        from core.utils.config import ConfigManager

        words = ConfigManager.instance().get_app_config("wakeup.keywords", [])
    except Exception as e:  # noqa: BLE001 - 词源缺失降级为不检查
        logger.warning(f"[Speaker] announce word source unavailable (keywords): {e}")
        return []
    return [str(w) for w in (words or []) if w]


def _config_command_keys() -> list[str]:
    """词源 2：免唤醒/截胡指令键（config 模块的 DIRECT/XIAOAI_COMMANDS）。

    与词源 1 各自维护、可能漂移（P2 才做「词表 ⊇ 指令表」启动校验），
    所以两组都查。config 模块未加载（部分测试环境）时按空处理。
    """
    module = sys.modules.get("config")
    if module is None:
        logger.debug(
            "[Speaker] announce word source unavailable (config module not loaded)"
        )
        return []
    keys: list[str] = []
    for attr in ("DIRECT_COMMANDS", "XIAOAI_COMMANDS"):
        table = getattr(module, attr, None)
        if isinstance(table, dict):
            keys.extend(str(k) for k in table if k)
    return keys


def find_blocked_word(text: str | None) -> str | None:
    """播报文案与两组词源交叉（子串包含，事故语义即「文案含词表词」）。

    命中返回该词；文案为空或词源全空返回 None。
    """
    if not text:
        return None
    for word in _kws_keywords() + _config_command_keys():
        if word and word in text:
            return word
    return None


class _AnnounceSession:
    """一次 announce 的观测句柄：等待完成的结局（测试与调用方可读）。"""

    __slots__ = ("wait_finished", "wait_started", "wait_reason")

    def __init__(self) -> None:
        self.wait_finished: bool | None = None
        self.wait_started: bool | None = None
        self.wait_reason: str | None = None


class SpeakerManager:
    """音箱硬件控制外观。

    所有设备命令都经 `core.device` 能力层发出（ticket 02），本类只做多步编排
    与对外方法签名；判据（合成落盘 / 回读 / 音量 / 麦语义）按设备真值（ticket 03），
    且只读能力层结构化结果（ok / readback）——业务层不做 stdout 字符串判据（ticket 01）。
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
            # 同步远端最新状态（能力层 readback：playing/paused/None）
            res = await self.device.playback_status()
            status = (res.readback or {}).get("status")
            if status in ("playing", "paused"):
                self.status = status
        return self.status

    async def set_playing(self, playing=True):
        """播放/暂停"""
        if playing:
            res = await self.device.playback_resume()
        else:
            res = await self.device.pause()
        return bool(res.ok)

    async def next_track(self) -> bool:
        """切到设备自身播放队列的下一首（桥自持队列由 song_request 处理）。"""
        return bool((await self.device.playback_next()).ok)

    async def prev_track(self) -> bool:
        """切到设备自身播放队列的上一首。"""
        return bool((await self.device.playback_prev()).ok)

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
                # 阻塞 URL 播放器的退出码是可靠判据（与设备 TTS 脚本不同，
                # 登记为 EXIT_CODE）；判据统一读能力层结构化 ok（ticket 01）。
                return bool(res.ok)
            return await self._play_text_blocking(text, timeout)

        if url:
            res = await self.device.play_url(url, timeout_ms=timeout)
        else:
            res = await self.device.tts_speak(text, timeout_ms=timeout)
        return bool(res.ok)

    async def _play_text_blocking(self, text, timeout_ms) -> bool:
        """阻塞播报：合成落盘 → 设备本地播放 → 回读空闲 → 清理临时文件。

        成功判据 = 合成成功 + 播放命令被接收 + 回读空闲（或超时兜底）；
        不再使用设备脚本退出码（设备 TTS 脚本成功时 rc 也是 1）。
        合成失败才回退到设备脚本，回退路径由能力层做「先 JSON、后 shell」双层转义。
        """
        content = text or "你好"

        # TurnTrace（ticket 11）：只记合成耗时与文案长度（字符数），不记文案。
        turn_trace.note_tts_chars(len(content))
        turn_trace.mark_tts_start()
        synth = await self.device.tts_synthesize(
            content, timeout_ms=_TTS_SYNTHESIS_TIMEOUT_MS
        )
        turn_trace.mark_tts_end()
        path = str((synth.readback or {}).get("path") or "")
        synth_ok = bool(synth.ok and path)
        turn_trace.note_tts_result(ok=synth_ok, fallback=not synth_ok)
        if not synth.ok or not path:
            logger.warning(
                f"[Speaker] TTS synthesis failed ({synth.error or 'no_path'}), "
                f"readback={synth.readback}, "
                "falling back to device script"
            )
            return await self._play_text_via_script(content, timeout_ms)

        play = await self.device.play_url(f"file://{path}")
        turn_trace.note_play_url(bool(play.ok))
        # 播放起点链（ticket 02）：调用返回时刻戳（含拒绝轮的请求耗时）——
        # tts_to_play / accept_to_start 的锚。
        turn_trace.mark_play_url()
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
        """兜底路径：设备阻塞 TTS 脚本。判据由能力层登记（EXIT_CODE_IN +
        stderr 解析错误检查），业务层只读结构化 `ok`。"""

        res = await self.device.play_text_blocking(text, timeout_ms=timeout_ms)
        if not res.ok:
            logger.warning(
                f"[Speaker] device script fallback rejected "
                f"(rc={res.exit_code}, error={res.error}, "
                f"stderr={res.stderr.strip()!r})"
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
        return bool(res.ok)

    async def ask_xiaoai(self, text: str, silent=False):
        """
        把文字指令交给原来的小爱执行

        参数:
            text: 文字指令
            silent: 是否静默执行
        """
        res = await self.device.ask_xiaoai(text, silent=silent)
        return bool(res.ok)

    async def abort_xiaoai(self):
        """
        中断原来小爱的运行

        注意：重启需要大约 1-2s 的时间，在此期间无法使用小爱音箱自带的 TTS 服务
        """
        res = await self.device.restart_xiaoai_service()
        return bool(res.ok)

    async def get_boot(self):
        """获取启动分区"""
        res = await self.device.boot_partition_get()
        return res.stdout.strip()

    async def set_boot(self, boot_part: Literal["boot0", "boot1"]):
        """设置启动分区（成败以能力层 fw_env -g 回读为准）。"""
        res = await self.device.boot_partition_set(boot_part)
        return bool(res.ok)

    async def get_device(self):
        """获取设备型号、序列号信息"""
        res = await self.device.device_info()
        info = res.stdout.strip().split(" ")
        return {
            "model": info[0] if len(info) > 0 else "unknown",
            "sn": info[1] if len(info) > 1 else "unknown",
        }

    async def get_mic(self):
        """获取麦克风状态（能力层 readback；读不到按 off 语义返回）。"""
        res = await self.device.mic_get()
        mic = (res.readback or {}).get("mic")
        return mic if mic in ("on", "off") else "off"

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

        与 `get_mic()` 走同一条 `mic_get` 通道，但把「读不到」与「off」区分开
        （能力层 readback 在设备无响应时给出 `mic: None`），
        供闸门的 fallback 判定使用（`get_mic()` 的对外语义保持不变）。
        """
        try:
            res = await self.device.mic_get()
        except Exception as e:  # noqa: BLE001 - 读不到只能降级
            logger.warning(f"[Speaker] mic snapshot read failed: {e}")
            return None
        mic = (res.readback or {}).get("mic")
        if mic not in ("on", "off"):
            logger.warning(
                f"[Speaker] mic snapshot unreadable: {res.error or res.exception}"
            )
            return None
        return mic

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

    # ------------------------------------------------------------------
    # announce：播报编排深模块（P0 spec C2 / ticket 02）
    #
    # 「朝音箱说话」的全套防护只活在这里：固件闭麦（快照恢复 + 串行锁 +
    # 看门狗）+ KWS 软闸门（引用计数 + 兜底 timer）+ 词表安全检查 +
    # 完成等待（事件适配 + 文本估算超时兜底），finally 保证双闸门归还。
    # 事故档案 docs/plan/incident-kws-self-trigger-loop.md §5 建议 1 的
    # 手工清单在这里转为自动化纪律；新播报入口一律经此接入。
    # ------------------------------------------------------------------

    def _gate_kws(self, kws: Any, reason: str, max_seconds: float) -> str | None:
        """开 KWS 播放闸门（失败不阻断播报，返回 None）。"""
        if not kws:
            return None
        try:
            return kws.gate_on(reason=reason, max_seconds=max_seconds)
        except Exception as e:  # noqa: BLE001 - 闸门失败只告警
            logger.warning(f"[Speaker] KWS gate_on failed ({reason}): {e}")
            return None

    def _ungate_kws(self, kws: Any, token: str | None) -> None:
        """关 KWS 播放闸门（失败不抛）。"""
        if not kws:
            return
        try:
            kws.gate_off(token)
        except Exception as e:  # noqa: BLE001 - finally 里不得抛
            logger.warning(f"[Speaker] KWS gate_off failed: {e}")

    @asynccontextmanager
    async def announce(
        self,
        text: str | None = None,
        *,
        reason: str = "announce",
        exempt: bool = False,
        wait: bool = True,
        gate_seconds: float | None = None,
        wait_budget_ms: float | None = None,
    ):
        """播报编排上下文管理器：进入做防护，退出恢复，finally 双闸门归还。

        进入（次序固定）：
          1. 取 mic_gate 串行锁（固件闭麦：快照恢复 + 看门狗）——先锁后开
             KWS 闸门，排队期间 KWS 兜底 timer 不空转，也不会出现「timer
             先期归还、真正播放时闸门已关」的洞；
          2. 开 KWS 软闸门（引用计数 + 兜底 timer）；
          3. 词表安全检查：文案与 KWS 关键词 + 免唤醒指令键交叉，命中默认
             拒绝（raise AnnounceRejected）并告警；`exempt=True` 显式豁免。

        body：经设备能力层播放（调用方负责，通常 `speaker.play`）。

        退出：`wait=True` 时等播放真正完成（事件适配 + 按文本估算的超时
        兜底），结局写入 yield 出的 session；无论 body 成败，finally 依次
        关 KWS 闸门、恢复麦态快照。

        参数：
            text: 播报文案（词表检查与等待预算的依据；URL/文件播报传 None）
            reason: 闸门原因标记（日志与 gate_status 可观测）
            exempt: 跳过词表检查（确知安全的调用方显式豁免）
            wait: 退出时是否等播放完成——阻塞式播放（play blocking=True）
                  已内含等完成，传 False 避免双重等待
            gate_seconds: 双闸门持有上限；缺省按文本估算 + 5s
            wait_budget_ms: 完成等待预算；缺省按文本估算 + 5s
        """
        if gate_seconds is None:
            gate_seconds = estimate_playback_budget_ms(text or "", None) / 1000.0 + 5.0
        if wait and wait_budget_ms is None:
            wait_budget_ms = estimate_playback_budget_ms(text or "", None) + 5_000.0
        session = _AnnounceSession()
        kws = get_kws()
        async with self.mic_gate(reason, max_seconds=gate_seconds):
            token = self._gate_kws(kws, reason, gate_seconds)
            try:
                if not exempt:
                    word = find_blocked_word(text)
                    if word is not None:
                        logger.warning(
                            f"[Speaker] announce rejected by word-list check "
                            f"(reason={reason!r}, word={word!r})"
                        )
                        raise AnnounceRejected(word)
                yield session
                if wait:
                    finished, started, wait_reason = (
                        await get_playback_events().wait_for_finish(wait_budget_ms)
                    )
                    session.wait_finished = finished
                    session.wait_started = started
                    session.wait_reason = wait_reason
                    if not finished:
                        logger.warning(
                            "[Speaker] announce completion unconfirmed "
                            f"(reason={reason!r}, wait_reason={wait_reason}, "
                            f"budget={wait_budget_ms:.0f}ms)"
                        )
                    elif wait_reason == "timeout":
                        logger.warning(
                            "[Speaker] announce completion not observed, "
                            f"timeout fallback (reason={reason!r})"
                        )
            finally:
                self._ungate_kws(kws, token)

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
        """设置音量（百分比 0-100），回读一致才算成功。

        夜间模式护栏（A-3/P3 票 01）：设备有非对称调容策略——日常时段只允许
        调大、夜间（22:05–07:05 volume=night）允许调小（设备真值见
        ws: docs/devices/lx06/nightmode-volume-policy.md §3）。bridge 不拦截
        请求（continuous_volume 通道是否受该策略纠正未证实），但失败时按
        语义归因：白天调小被回读拒绝会被点名为夜间模式语义，而非未知 bug。
        """
        before = await self.get_volume()
        res = await self.device.volume_set(int(level))
        if not res.ok:
            await self._log_volume_failure_with_nightmode(level, before, res.error)
        return bool(res.ok)

    async def _log_volume_failure_with_nightmode(
        self, level: int, before: int | None, error: str | None
    ) -> None:
        """音量写失败时按夜间模式语义归因（诊断增强，不改成败判定）。"""
        try:
            night = await self.device.nightmode_current()
            volume_mode = (night.readback or {}).get("volume") if night.readback else None
        except Exception:  # noqa: BLE001 - 诊断路径不得引入新失败
            volume_mode = None
        if (
            volume_mode == "normal"
            and before is not None
            and before >= 0
            and int(level) < before
        ):
            logger.warning(
                f"[Speaker] set_volume({level}) rejected (readback mismatch): "
                f"日常时段（nightmode volume=normal）设备只允许调大音量——"
                f"夜间模式非对称调容语义，非 bug（当前 {before}）"
            )
            return
        logger.warning(f"[Speaker] set_volume({level}) failed: {error}")

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
