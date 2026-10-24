import asyncio
import threading
import time

import numpy as np
import open_xiaoai_server

from core.ref import get_speaker, set_xiaoai
from core.services.audio.channels import ChannelDownmixer, normalize_strategy
from core.services.audio.stream import GlobalStream
from core.services.audio.watchdog import (
    STATE_IDLE,
    STATE_RECOVER,
    STATE_WARNING,
    RecordingWatchdog,
)
from core.services.speaker import SpeakerManager
from core.wakeup_session import EventManager
from core.xiaoai_conversation import XiaoAIConversationController
from core.utils.base import json_decode
from core.utils.config import ConfigManager
from core.utils.logger import logger

ASCII_BANNER = """
▄▖      ▖▖▘    ▄▖▄▖
▌▌▛▌█▌▛▌▚▘▌▀▌▛▌▌▌▐ 
▙▌▙▌▙▖▌▌▌▌▌█▌▙▌▛▌▟▖
  ▌                
                                                                                                                
"""


class XiaoAI:
    speaker = SpeakerManager()
    async_loop: asyncio.AbstractEventLoop = None
    config_manager = ConfigManager.instance()
    conversation = XiaoAIConversationController()
    _input_gain_enabled = False
    _input_gain = 1.0
    _async_loop_ready = threading.Event()
    _external_wakeup_keywords: set[str] = set()
    _suppressed_dialog_ids: set[str] = set()
    _suppressed_dialog_last_attempt: dict[str, float] = {}
    _MAX_SUPPRESSED_DIALOGS = 100
    _SUPPRESS_RETRY_INTERVAL = 0.35

    # ---- 多声道采集 + 录音看门狗（ticket 04）----
    # client 录音读循环 500ms 无数据即退出且不通知服务端，故 1s 无帧判死。
    _WATCHDOG_INTERVAL = 0.5
    _RECOVER_BASE_BACKOFF = 1.0
    _RECOVER_MAX_BACKOFF = 30.0
    _audio_channels = 6
    _channel_strategy = "strongest"
    _fixed_channel = 0
    _downmixer: ChannelDownmixer | None = None
    _last_input_frame_time = 0.0
    _input_frame_count = 0
    _current_channel = None
    _recording_expected = False
    _recording_stop_requested = False
    _recording_watchdog = RecordingWatchdog()
    _watchdog_task = None
    _watchdog_warned = False
    _recovering = False
    _recover_failures = 0
    _next_recover_at = 0.0
    _last_recover_error = None

    @classmethod
    def refresh_runtime_config(cls, *_args):
        """从配置中心同步运行时参数。"""
        cls.conversation.apply_runtime_config(
            cls.config_manager.get_app_config("xiaoai", {})
        )
        gain = cls.config_manager.get_app_config("audio_input.gain", 1.0)
        try:
            gain = float(gain)
        except (TypeError, ValueError):
            gain = 1.0
        cls._input_gain = max(1.0, min(gain, 8.0))
        cls._input_gain_enabled = cls._input_gain > 1.0
        cls._apply_audio_input_config()
        wakeup_keywords = cls.config_manager.get_app_config("wakeup.keywords", [])
        cls._external_wakeup_keywords = {
            cls._normalize_text(keyword)
            for keyword in wakeup_keywords
            if isinstance(keyword, str) and cls._normalize_text(keyword)
        }

    @classmethod
    def _apply_audio_input_config(cls):
        """同步六麦采集配置（声道数 / 降声道策略 / 看门狗阈值）。"""
        try:
            channels = int(cls.config_manager.get_app_config("audio_input.channels", 6))
        except (TypeError, ValueError):
            channels = 6
        channels = max(1, min(channels, 8))

        strategy = normalize_strategy(
            cls.config_manager.get_app_config(
                "audio_input.channel_strategy", "strongest"
            )
        )
        try:
            fixed_channel = int(
                cls.config_manager.get_app_config("audio_input.fixed_channel", 0)
            )
        except (TypeError, ValueError):
            fixed_channel = 0
        fixed_channel = max(0, min(fixed_channel, channels - 1))

        try:
            watchdog_seconds = float(
                cls.config_manager.get_app_config("audio_input.watchdog_seconds", 1.0)
            )
        except (TypeError, ValueError):
            watchdog_seconds = 1.0
        watchdog_seconds = max(0.2, min(watchdog_seconds, 10.0))

        changed = (
            channels != cls._audio_channels
            or strategy != cls._channel_strategy
            or fixed_channel != cls._fixed_channel
        )
        cls._audio_channels = channels
        cls._channel_strategy = strategy
        cls._fixed_channel = fixed_channel
        cls._recording_watchdog.configure(watchdog_seconds)

        if changed or cls._downmixer is None:
            cls._downmixer = (
                None
                if channels == 1
                else ChannelDownmixer(
                    channels=channels,
                    strategy=strategy,
                    fixed_channel=fixed_channel,
                )
            )
            cls._current_channel = (
                cls._downmixer.current_channel if cls._downmixer else None
            )

    @staticmethod
    def _normalize_text(text: str) -> str:
        if not isinstance(text, str):
            return ""
        return "".join(text.strip().lower().split())

    @classmethod
    def _is_external_wakeup_text(cls, text: str) -> bool:
        normalized = cls._normalize_text(text)
        return bool(normalized) and normalized in cls._external_wakeup_keywords

    # ---- 注入标记（firmware-asr-injection-feasibility.md §3.4 机制 B）----
    INJECTION_MARKER = "注入输入"

    @classmethod
    def _strip_injection_marker(cls, text: str) -> str | None:
        """命中注入前缀则返回剥离后的内容，否则 None。

        宽松匹配：允许 ASR 把标记识别成同音/近形词、或标记后带标点/空格。
        不匹配时返回 None，不影响正常语音流。
        """
        if not text:
            return None
        t = text.strip()
        for head in ("注入输入", "朱入输入", "注入书入", "主入输入", "柱入输入"):
            if t.startswith(head):
                return t[len(head):].lstrip(" ，,。.、").strip()
        return None

    @staticmethod
    def _redact_inject_text(text: str, limit: int = 40) -> str:
        """注入内容日志脱敏：仅截断，不写完整内容。"""
        t = (text or "").strip().replace("\n", " ")
        return t[:limit] + ("…" if len(t) > limit else "")

    @classmethod
    async def _suppress_dialog(cls, dialog_id: str, reason: str):
        if not dialog_id:
            return

        # Prevent unbounded growth: if too many stale dialog_ids accumulated
        # (missed Dialog.Finish events), clear them all before adding new one
        if len(cls._suppressed_dialog_ids) >= cls._MAX_SUPPRESSED_DIALOGS:
            logger.debug(
                f"[XiaoAI] Clearing {len(cls._suppressed_dialog_ids)} stale suppressed dialog_ids"
            )
            cls._suppressed_dialog_ids.clear()
            cls._suppressed_dialog_last_attempt.clear()

        is_new_dialog = dialog_id not in cls._suppressed_dialog_ids
        cls._suppressed_dialog_ids.add(dialog_id)

        if is_new_dialog:
            logger.info(
                f"[XiaoAI] 🛑 停止小爱当前对话: {reason}"
            )

        now = time.monotonic()
        last_attempt = cls._suppressed_dialog_last_attempt.get(dialog_id, 0.0)
        if not is_new_dialog and (now - last_attempt) < cls._SUPPRESS_RETRY_INTERVAL:
            return

        cls._suppressed_dialog_last_attempt[dialog_id] = now
        try:
            await cls.speaker.stop_local_playback()
            if is_new_dialog:
                await cls.speaker.wake_up(awake=False)
        except Exception as exc:
            logger.debug(
                f"[XiaoAI] Failed to pause suppressed dialog {dialog_id}: {exc}"
            )

    @classmethod
    def on_input_data(cls, data: bytes):
        if not data:
            return

        # 只要字节在流动就刷新看门狗基线（残留未满一帧也算录音通道活着）。
        cls._last_input_frame_time = time.monotonic()
        # 连接建立时 native 的 test() 会自行 start_recording（不经 Python）；
        # 首帧到达即视为「录音应持续」，否则看门狗永远不接管这条初始通道。
        # 显式 stop（TTS 防回声/注入窗口）之后的在途帧不得重新武装：
        # 若在此误置 True，TTS 播放超过 1s 就会触发恢复把录音打开（回声）。
        if not cls._recording_expected and not cls._recording_stop_requested:
            cls._recording_expected = True

        downmixer = cls._downmixer
        if downmixer is not None:
            pcm = downmixer.feed(data)
            cls._current_channel = downmixer.current_channel
            if not pcm:
                # 跨帧残留未凑满一个声道帧，等待下一次回调
                return
        else:
            # 单声道回退：保持旧行为（仅丢弃尾部半个样本，避免 frombuffer 抛错）
            pcm = data if len(data) % 2 == 0 else data[:-1]

        audio_array = np.frombuffer(pcm, dtype=np.int16)
        if audio_array.size == 0:
            return
        if cls._input_gain_enabled:
            boosted = audio_array.astype(np.float32) * cls._input_gain
            audio_array = np.clip(boosted, -32768, 32767).astype(np.int16)
        cls._input_frame_count += 1
        GlobalStream.input(audio_array.tobytes())

    # ---- 录音通道：native 调用的唯一包装（看门狗 expected 标志由这里维护）----

    @classmethod
    async def start_recording(cls) -> bool:
        """恢复设备端录音；返回是否成功（异常不外抛，交给看门狗重试）。"""
        cls._recording_expected = True
        cls._recording_stop_requested = False
        cls._last_input_frame_time = time.monotonic()
        cls._recording_watchdog.mark_started()
        cls._watchdog_warned = False
        try:
            await open_xiaoai_server.start_recording()
            return True
        except Exception as exc:
            cls._last_recover_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"[XiaoAI] start_recording 失败: {cls._last_recover_error}"
            )
            return False

    @classmethod
    async def stop_recording(cls) -> bool:
        """停止设备端录音（预期停止：TTS 防回声 / 注入窗口），看门狗不再告警。"""
        cls._recording_expected = False
        cls._recording_stop_requested = True
        cls._watchdog_warned = False
        cls._recording_watchdog.evaluate(None, time.monotonic(), False)
        try:
            await open_xiaoai_server.stop_recording()
            return True
        except Exception as exc:
            cls._last_recover_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                f"[XiaoAI] stop_recording 失败: {cls._last_recover_error}"
            )
            return False

    @classmethod
    async def _recording_watchdog_loop(cls):
        """后台巡检：录音通道无帧 → 先停后启（带退避）。"""
        logger.info(
            "[XiaoAI] 录音看门狗已启动"
            f"（{cls._recording_watchdog.timeout_seconds:.1f}s 无帧判定）"
            f" channels={cls._audio_channels}"
            f" strategy={cls._channel_strategy}"
            f" fixed_channel={cls._fixed_channel}"
        )
        while True:
            await asyncio.sleep(cls._WATCHDOG_INTERVAL)
            try:
                await cls._check_recording_health(time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    f"[XiaoAI] 录音看门狗检查异常: {type(exc).__name__}: {exc}"
                )

    @classmethod
    async def _check_recording_health(cls, now: float):
        state = cls._recording_watchdog.evaluate(
            cls._last_input_frame_time or None,
            now,
            cls._recording_expected,
        )
        if state == STATE_IDLE:
            cls._watchdog_warned = False
            return
        if state == STATE_WARNING:
            if not cls._watchdog_warned:
                cls._watchdog_warned = True
                age = now - cls._last_input_frame_time
                logger.warning(
                    f"[XiaoAI] 录音通道 {age:.2f}s 无音频帧"
                    f"（阈值 {cls._recording_watchdog.timeout_seconds:.1f}s）"
                )
            return
        if state == STATE_RECOVER:
            if cls._recovering or now < cls._next_recover_at:
                return
            await cls._recover_recording(now)

    @classmethod
    async def _recover_recording(cls, now: float):
        """先停后启；失败按指数退避重试，全部走结构化日志。"""
        cls._recovering = True
        age = now - cls._last_input_frame_time if cls._last_input_frame_time else None
        age_text = f"{age:.2f}s" if age is not None else "unknown"
        logger.warning(
            "[XiaoAI] 录音恢复开始: "
            f"reason=no_frames age={age_text} attempt={cls._recover_failures + 1} "
            f"channels={cls._audio_channels} strategy={cls._channel_strategy}"
        )
        try:
            stopped = await cls.stop_recording()
            started = await cls.start_recording()
            if not (stopped and started):
                raise RuntimeError(cls._last_recover_error or "recording restart failed")
        except Exception as exc:
            cls._recover_failures += 1
            cls._last_recover_error = f"{type(exc).__name__}: {exc}"
            backoff = min(
                cls._RECOVER_BASE_BACKOFF * (2 ** (cls._recover_failures - 1)),
                cls._RECOVER_MAX_BACKOFF,
            )
            cls._next_recover_at = time.monotonic() + backoff
            logger.error(
                "[XiaoAI] 录音恢复失败: "
                f"attempt={cls._recover_failures} error={cls._last_recover_error} "
                f"retry_in={backoff:.1f}s"
            )
        else:
            cls._recover_failures = 0
            cls._last_recover_error = None
            cls._next_recover_at = 0.0
            cls._last_input_frame_time = time.monotonic()
            cls._watchdog_warned = False
            count = cls._recording_watchdog.notify_recovered()
            logger.info(
                "[XiaoAI] 录音恢复完成: "
                f"recover_count={count} channels={cls._audio_channels} "
                f"strategy={cls._channel_strategy}"
            )
        finally:
            cls._recovering = False

    @classmethod
    def audio_health(cls) -> dict:
        """录音面健康快照（供 /api/health、管理面板与排障使用）。"""
        now = time.monotonic()
        last = cls._last_input_frame_time
        return {
            "recording_expected": bool(cls._recording_expected),
            "last_frame_age": round(max(0.0, now - last), 3) if last else None,
            "frame_count": cls._input_frame_count,
            "channels": cls._audio_channels,
            "channel_strategy": cls._channel_strategy,
            "fixed_channel": cls._fixed_channel,
            "current_channel": cls._current_channel,
            "watchdog_state": cls._recording_watchdog.state,
            "recover_count": cls._recording_watchdog.recover_count,
            "last_recover_error": cls._last_recover_error,
        }

    @classmethod
    def on_output_data(cls, data: bytes):
        async def on_output_data_async(data: bytes):
            return await open_xiaoai_server.on_output_data(data)

        asyncio.run_coroutine_threadsafe(
            on_output_data_async(data),
            cls.async_loop,
        )

    @classmethod
    async def run_shell(cls, script: str, timeout: float = 10 * 1000):
        return await open_xiaoai_server.run_shell(script, timeout)

    @classmethod
    async def on_event(cls, event: str):
        event_json = json_decode(event) or {}
        if not isinstance(event_json, dict):
            logger.debug("[XiaoAI] 忽略非字典事件负载")
            return

        event_data = event_json.get("data", {})
        event_type = event_json.get("event")

        if not event_json.get("event"):
            return

        # 记录所有事件用于调试监听退出
        logger.debug(f"[XiaoAI] 📡 收到事件: {event_type} | 数据: {event_data}")

        if event_type == "instruction":
            if not isinstance(event_data, dict):
                logger.debug(
                    f"[XiaoAI] 忽略非字典 instruction 数据: {event_data}"
                )
                return

            raw_line = event_data.get("NewLine")
            if not raw_line:
                return

            line = json_decode(raw_line) if isinstance(raw_line, str) else raw_line
            if not isinstance(line, dict):
                logger.debug(f"[XiaoAI] 忽略无法解析的指令行: {raw_line}")
                return

            header = line.get("header", {}) if isinstance(line.get("header"), dict) else {}
            dialog_id = header.get("dialog_id", "")
            namespace = header.get("namespace")
            header_name = header.get("name")

            if dialog_id and dialog_id in cls._suppressed_dialog_ids:
                if namespace in {"Nlp", "SpeechSynthesizer", "AudioPlayer"}:
                    await cls._suppress_dialog(
                        dialog_id,
                        f"{namespace}.{header_name}",
                    )
                    return
                if namespace == "Dialog" and header_name == "Finish":
                    cls._suppressed_dialog_ids.discard(dialog_id)
                    cls._suppressed_dialog_last_attempt.pop(dialog_id, None)
                    logger.debug(
                        f"[XiaoAI] Cleared suppressed dialog: {dialog_id}"
                    )
                    return

            if (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "SpeechRecognizer"
            ):
                if header_name == "RecognizeResult":
                    payload = line.get("payload", {})
                    if not isinstance(payload, dict):
                        logger.debug(
                            f"[XiaoAI] 忽略非字典 RecognizeResult payload: {payload}"
                        )
                        return

                    results = payload.get("results") or []
                    first_result = results[0] if results else {}
                    if isinstance(first_result, dict):
                        text = first_result.get("text") or ""
                    elif isinstance(first_result, str):
                        text = first_result
                    else:
                        text = ""
                    is_final = payload.get("is_final")
                    is_vad_begin = payload.get("is_vad_begin")

                    # ---- 注入防护（firmware-asr-injection-feasibility.md §3.4）----
                    # 闸门(A)：PC 注入期间吞掉所有 RecognizeResult，防注入内容被当
                    # 用户指令进 Agent/对话后端。必须在此之前 return，否则会落入下
                    # 方分支被转交 handle_text_command。
                    if EventManager.injection_gate_active():
                        logger.info(
                            f"[Injection] 闸门内丢弃 ASR 结果: {cls._redact_inject_text(text)}"
                        )
                        # 文本回传：广播给 WS 订阅者（PC 润色后插入光标）
                        # is_final 必须透传（真机实测：一句话会产生多条 RecognizeResult，
                        # 且为累积快照；中间快照供「边说边出字」，is_final=true 才是最终结果）。
                        try:
                            from core.services.api_server import get_relay
                            relay = get_relay()
                            if relay:
                                relay.publish_text(text or "", is_final=bool(is_final))
                        except Exception:
                            pass
                        return

                    if EventManager.consume_xiaoai_asr_result(
                        dialog_id=dialog_id,
                        text=text,
                        is_final=is_final,
                        is_vad_begin=is_vad_begin,
                    ):
                        if dialog_id and text and is_final:
                            await cls._suppress_dialog(
                                dialog_id,
                                f"Bridge 接管原生 ASR",
                            )
                        return

                    # 标记(B)：注入音频自带的可识别前缀（双保险，闸门失效时仍可拦截）。
                    # 命中后剥前缀，仅记录（后续可经 WS/SSE 回传 PC，不进 Agent）。
                    stripped = cls._strip_injection_marker(text or "")
                    if stripped is not None:
                        logger.info(
                            f"[Injection] 标记命中(闸门外)，已拦截: {cls._redact_inject_text(stripped)}"
                        )
                        if dialog_id and is_final:
                            await cls._suppress_dialog(
                                dialog_id,
                                "注入标记拦截",
                            )
                        return
                    
                    # 只有明确的 is_vad_begin=False 且没有文本时才触发唤醒
                    # 避免重复触发
                    if not text and is_vad_begin is False:
                        logger.wakeup("小爱同学", module="XiaoAI")
                        cls.conversation.reset_retries()
                        EventManager.on_interrupt()
                    elif text and is_final and cls._is_external_wakeup_text(text):
                        await cls._suppress_dialog(
                            dialog_id,
                            f"外部唤醒词接管: {text}",
                        )
                        await EventManager.wakeup(text, "kws")
                        return
                    elif text and is_final:
                        logger.info(f"[XiaoAI] 🔥 收到指令: {text}")
                        cls.conversation.reset_retries()
                        await cls.conversation.handle_text_command(
                            text,
                            get_speaker(),
                        )
                        await EventManager.wakeup(text, "xiaoai")
                    elif is_final and not text:
                        logger.debug("[XiaoAI] 🛑 小爱监听超时自动退出")
                        await cls.conversation.handle_listening_timeout(
                            get_speaker()
                        )
            elif (
                line
                and isinstance(line.get("header"), dict)
                and namespace == "AudioPlayer"
            ):
                cls.conversation.handle_audio_player_instruction(header_name)
        elif event_type == "playing":
            if not isinstance(event_data, str):
                logger.debug(
                    f"[XiaoAI] 忽略非字符串 playing 数据: {event_data}"
                )
                return

            playing_status = event_data.lower()
            
            get_speaker().status = playing_status
            await cls.conversation.handle_playing_status(
                playing_status,
                get_speaker(),
            )
        
        else:
            # 记录未处理的事件类型，可能包含监听退出信息
            logger.debug(f"[XiaoAI] ❓ 未处理的事件类型: {event_type} | 完整数据: {event_json}")

    @classmethod
    def __init_background_event_loop(cls):
        def run_event_loop():
            cls.async_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(cls.async_loop)
            cls._async_loop_ready.set()
            cls.async_loop.run_forever()

        thread = threading.Thread(target=run_event_loop, daemon=True)
        thread.start()
        cls._async_loop_ready.wait(timeout=2)

    @classmethod
    def __on_event(cls, event: str):
        future = asyncio.run_coroutine_threadsafe(
            cls.on_event(event),
            cls.async_loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(
                    f"[XiaoAI] Event handler failed: {type(exc).__name__}: {exc}"
                )

        future.add_done_callback(_log_result)

    @classmethod
    async def init_xiaoai(cls):
        cls.refresh_runtime_config()
        cls.config_manager.add_reload_listener(cls.refresh_runtime_config)
        set_xiaoai(XiaoAI)
        GlobalStream.on_output_data = cls.on_output_data
        open_xiaoai_server.register_fn("on_input_data", cls.on_input_data)
        open_xiaoai_server.register_fn("on_event", cls.__on_event)
        cls.__init_background_event_loop()
        # 看门狗挂在专用 async_loop 上；start_server 之后的代码可能永不返回
        cls._watchdog_task = asyncio.run_coroutine_threadsafe(
            cls._recording_watchdog_loop(),
            cls.async_loop,
        )
        logger.info("[XiaoAI] 启动小爱音箱服务...")
        print(ASCII_BANNER)
        await open_xiaoai_server.start_server()

    @classmethod
    def stop_conversation(cls):
        cls.conversation.stop()
