"""External backend continuous conversation controller.

After a custom wake word triggers wakeup, this module drives either:
  - local VAD -> ASR -> backend -> TTS
  - XiaoAI native ASR -> backend -> TTS

The selected input path runs independently of the XiaoZhi session state
machine.

Key design decisions:
  - Uses per-session asyncio.Future objects so it never conflicts with the
    XiaoZhi wakeup session state machine.
  - Never calls abort_xiaoai() (which would break the FileMonitor).
  - TTS playback is blocking (awaited), so the next listening round
    only starts after the response has finished playing.
"""

import asyncio
import os
import time

import open_xiaoai_server

from core.ref import get_speaker, get_vad
from core.utils.config import ConfigManager
from core.utils import pipeline as conversation_pipeline
from core.utils.pipeline import (
    CHECKPOINT_ASR_FINAL,
    CHECKPOINT_PLAYBACK,
    CHECKPOINT_PRE_LLM,
    CHECKPOINT_STREAM_SEGMENT,
    PipelineState,
    TurnCancelledError,
)

_NOTIFY_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "tts_notify.mp3",
)

_SEND_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "assets", "sounds", "send_notify.mp3",
)

def _load_notify_sound() -> bytes | None:
    """Decode tts_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_NOTIFY_SOUND_PATH):
        return None
    try:
        with open(_NOTIFY_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

def _load_send_sound() -> bytes | None:
    """Decode send_notify.mp3 to PCM at startup."""
    if not os.path.isfile(_SEND_SOUND_PATH):
        return None
    try:
        with open(_SEND_SOUND_PATH, "rb") as f:
            mp3_data = f.read()
        return open_xiaoai_server.decode_audio(mp3_data, format="mp3", sample_rate=24000)
    except Exception:
        return None

_NOTIFY_PCM = _load_notify_sound()
_SEND_PCM = _load_send_sound()
from core.utils.logger import logger
from core.utils import turn_trace


class ExternalConversationController:
    """Manages multi-turn conversation for an external backend."""

    LOCAL_ASR_INPUT = "local_asr"
    XIAOAI_ASR_INPUT = "xiaoai_asr"
    XIAOAI_ASR_TIMEOUT = "__timeout__"
    CONFIG_PREFIX = "openclaw"
    BACKEND_NAME = "OpenClaw"
    LOG_MODULE = "OpenClaw Conv"
    WAKEUP_SOURCE = "openclaw"
    MANAGER = None

    def __init__(self):
        self.config = ConfigManager.instance()
        if self.MANAGER is None:
            raise RuntimeError("MANAGER must be set by subclass")
        self.backend = self.MANAGER
        self.active = False

        # Per-session asyncio.Future used to receive VAD events
        self._vad_future: asyncio.Future | None = None
        # Per-session asyncio.Future used to receive XiaoAI native ASR results
        self._xiaoai_asr_future: asyncio.Future | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Playback token for the current TTS session
        self._playback_token: int | None = None
        # 当前对话轮（pipeline 轮次标识；循环协程写、事件线程读，见 consume 回调）
        self._pipeline_turn = None
        # 本地链路「说完」时刻（VAD silence hook 写入，ASR 定稿后消费）
        self._speech_end_at: float | None = None

    # ---- config helpers ----

    def _cfg(self, key: str, default=None):
        return self.config.get_app_config(f"{self.CONFIG_PREFIX}.{key}", default)

    @property
    def exit_keywords(self) -> list[str]:
        return self._cfg("exit_keywords", ["退出", "停止", "再见"])

    @property
    def timeout(self) -> int:
        return int(self.config.get_app_config("wakeup.timeout", 20))

    @property
    def input_mode(self) -> str:
        mode = self._cfg("input_mode", self.LOCAL_ASR_INPUT)
        if not isinstance(mode, str):
            return self.LOCAL_ASR_INPUT
        normalized = mode.strip().lower()
        if normalized in {self.LOCAL_ASR_INPUT, self.XIAOAI_ASR_INPUT}:
            return normalized
        logger.warning(
            f"Unknown {self.CONFIG_PREFIX}.input_mode={mode!r}, fallback to {self.LOCAL_ASR_INPUT}",
            module=self.LOG_MODULE,
        )
        return self.LOCAL_ASR_INPUT

    def uses_xiaoai_asr(self) -> bool:
        return self.input_mode == self.XIAOAI_ASR_INPUT

    # ---- public API ----

    def is_active(self) -> bool:
        return self.active

    async def start(self):
        """Enter OpenClaw conversation mode."""
        if self.active:
            logger.warning(f"[{self.LOG_MODULE}] Already active, ignoring start()")
            return
        self.active = True
        self._loop = asyncio.get_running_loop()

        logger.info(f"🎙️ 进入 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

        try:
            await self._conversation_loop()
        except Exception as exc:
            import traceback
            logger.error(
                f"Conversation loop error: {type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()}",
                module=self.LOG_MODULE,
            )
        finally:
            self.stop()

    def stop(self):
        """Exit conversation mode and clean up."""
        if not self.active:
            return
        self.active = False
        self._cancel_vad_future()
        self._cancel_xiaoai_asr_future()
        if self._playback_token is not None:
            open_xiaoai_server.stop_tts_playback(self._playback_token)
            self._playback_token = None
        if self.uses_xiaoai_asr():
            speaker = get_speaker()
            if speaker and self._loop:
                try:
                    asyncio.run_coroutine_threadsafe(
                        speaker.wake_up(awake=False),
                        self._loop,
                    )
                except Exception as exc:
                    logger.debug(
                        f"Failed to stop XiaoAI native listening: {exc}",
                        module=self.LOG_MODULE,
                    )
        logger.info(f"👋 退出 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

    # ---- conversation loop ----

    async def _conversation_loop(self):
        """Run VAD -> ASR -> backend -> TTS turns until exit."""

        # Mute mic → play notify → unmute.
        # _play_notify() blocks for ~740ms (the beep duration), during which
        # the mic is off and before_wakeup TTS echo naturally fades.
        # VAD.resume() resets all state (speech_frames, input_bytes),
        # so speech detection starts clean when listening begins.
        await self._stop_recording()
        logger.debug("Recording stopped", module=self.LOG_MODULE)
        await self._play_notify()
        await self._start_recording()
        logger.debug("Ready to listen", module=self.LOG_MODULE)

        while self.active:
            # 每轮对话一个轮次标识：贯穿管线态日志与 TurnTrace 埋点（ticket 02）。
            turn_source = (
                self.XIAOAI_ASR_INPUT
                if self.uses_xiaoai_asr()
                else self.LOCAL_ASR_INPUT
            )
            turn = conversation_pipeline.begin_turn(turn_source)
            self._pipeline_turn = turn
            outcome = "played"
            try:
                if self.uses_xiaoai_asr():
                    result = await self._run_one_turn_with_xiaoai_asr()
                else:
                    result = await self._run_one_turn_with_local_asr()
                if result in ("exit", "timeout"):
                    outcome = result
                    if self.uses_xiaoai_asr():
                        await self._stop_xiaoai_native_listening()
                    await self._call_after_wakeup()
                    break
                elif result == "error":
                    outcome = "error"
                    break
            except TurnCancelledError as exc:
                # 固定检查点发现本轮已被取消（唤醒打断等）：静默退出本轮——
                # 不再发请求、不播后续句段；会话层 stop()/打断清理照常由
                # wakeup_session.on_interrupt 的既有两层机制完成。
                outcome = f"cancelled:{exc.checkpoint}"
                logger.info(
                    f"turn={exc.turn_id} silent exit at checkpoint={exc.checkpoint} "
                    f"reason={exc.reason}",
                    module=self.LOG_MODULE,
                )
                break
            finally:
                # 打断也可能以 asyncio.CancelledError 落地（未过检查点）：
                # 轮已取消时 outcome 细化为 cancelled，再按轮引用收尾——
                # 晚到的旧 finally 不会误杀新会话的新轮。
                if outcome == "played" and turn is not None and turn.cancelled:
                    outcome = "cancelled"
                self._pipeline_turn = None
                self._speech_end_at = None
                conversation_pipeline.end_turn(outcome, turn=turn)

    async def _run_one_turn_with_local_asr(self) -> str:
        """Execute a single conversation turn.

        Returns:
            "continue" - turn completed, loop to next
            "exit"     - user said an exit keyword
            "timeout"  - no speech detected within timeout
            "error"    - unrecoverable error
        """
        vad = get_vad()
        if not vad:
            logger.error("VAD not available", module=self.LOG_MODULE)
            return "error"

        # 1. Start listening for speech (recording is already active)
        speech_bytes = await self._wait_for_speech(vad)
        if speech_bytes is None:
            return "timeout"

        turn = self._pipeline_turn
        logger.debug(
            f"turn={turn.turn_id if turn else '-'} "
            f"Got speech buffer: {len(speech_bytes)} bytes",
            module=self.LOG_MODULE,
        )

        # 2. ASR: convert speech to text
        from core.services.audio.asr import ASRService

        text = ASRService.asr(speech_bytes, sample_rate=16000)
        if not text:
            logger.debug("ASR empty, retrying", module=self.LOG_MODULE)
            return "continue"

        # 检查点①（ASR 定稿后）：本轮已取消则静默退出（不发 LLM 请求）。
        self._checkpoint(CHECKPOINT_ASR_FINAL)
        # TurnTrace（ticket 02）：本地链路建轮于 ASR 定稿——带上「说完」时刻，
        # 补齐 speech_end → asr_final 段（轮次号与 pipeline 同源）。
        trace = turn_trace.begin_turn(
            "local_asr",
            asr_chars=len(text),
            turn_id=turn.turn_id if turn else None,
        )
        if trace is not None:
            if self._speech_end_at is not None:
                trace.mark("speech_end", self._speech_end_at)
            trace.mark("asr_final")

        # 3. Check exit keywords
        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                # TurnTrace（ticket 11）：退出轮没有 LLM/TTS，立即收尾为 incomplete。
                turn_trace.abandon_turn()
                return "exit"

        # 检查点②（LLM 请求前）+ 管线态 → processing
        self._enter_processing()

        # 4. Send to backend and speak the response as a segment stream
        #    (P1 ticket 04): streaming backends yield sentence segments as
        #    they arrive — the first segment starts TTS immediately while the
        #    LLM keeps generating; non-streaming backends degrade to a
        #    single-segment sequence via the base class, which collects to
        #    completion before the one and only playback — behavior identical
        #    to the pre-streaming path (stream-off rollback).
        #    「咻」与非流式时序相当：pump 先行调度，提示音播放让出控制权后
        #    请求即上路（等价于旧 _send_and_track 的先发后播）。
        full_text = text
        if self.backend._rule_prompt:
            full_text = text + "\n" + self.backend._rule_prompt
        segments = await self._speak_response_segments(full_text, with_send_sound=True)
        if not segments:
            logger.warning(f"No response from {self.BACKEND_NAME}", module=self.LOG_MODULE)
            speaker = get_speaker()
            if speaker:
                await speaker.play(text="抱歉，我没有收到回复")
            return "continue"

        # 5. Notify → Start recording → Wait for silence (mic was muted by
        #    _speak_response_segments before the first segment).
        #    _play_notify() blocks for ~740ms (the beep duration),
        #    enough for TTS echo to fade. After starting recording, we wait
        #    for silence to ensure any residual echo or buffered audio clears.
        #    VAD.resume() resets all state, so speech detection starts clean.
        await self._play_notify()
        await self._start_recording()
        logger.debug("Recording started, waiting for silence...", module=self.LOG_MODULE)
        await self._wait_for_silence(vad)
        logger.debug("Ready to listen", module=self.LOG_MODULE)

        return "continue"

    async def _run_one_turn_with_xiaoai_asr(self) -> str:
        """Execute a single conversation turn using XiaoAI native ASR."""
        text = await self._wait_for_xiaoai_asr_text()
        if text is None:
            logger.debug("XiaoAI native ASR turn timed out", module=self.LOG_MODULE)
            return "timeout"

        # 检查点①在 consume 回调（ASR 定稿处，事件线程）以非抛式完成；
        # 循环侧醒来后再过一次检查点（取消则静默退出，不发 LLM 请求）。
        self._checkpoint(CHECKPOINT_ASR_FINAL)

        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                # TurnTrace（ticket 11）：退出轮没有 LLM/TTS，立即收尾为 incomplete。
                turn_trace.abandon_turn()
                return "exit"

        self._enter_processing()

        full_text = text
        if self.backend._rule_prompt:
            full_text = text + "\n" + self.backend._rule_prompt
        segments = await self._speak_response_segments(full_text, with_send_sound=False)
        if not segments:
            logger.warning(f"No response from {self.BACKEND_NAME}", module=self.LOG_MODULE)
            speaker = get_speaker()
            if speaker:
                await speaker.play(text="抱歉，我没有收到回复")
            return "continue"
        await self._play_notify()
        await self._start_recording()
        logger.debug("Ready for next XiaoAI native ASR round", module=self.LOG_MODULE)
        return "continue"

    # ---- 管线态与取消传播（ticket 02）----

    def _checkpoint(self, name: str) -> None:
        """异步检查点：当前轮已取消时抛 TurnCancelledError（无轮时直通）。"""
        turn = self._pipeline_turn
        if turn is not None:
            turn.checkpoint(name)

    async def _speak_response_segments(
        self, text: str, *, with_send_sound: bool
    ) -> list[str]:
        """边生成边播句段（P1 ticket 04 主体，两条 ASR 链路共用）。

        结构：pump 协程立即驱动后端句段流（LLM 请求先上路），句段经
        队列交给本协程的播放循环——首段到达即断录起播（回声防护仍是
        物理断录，只是时点从「全部收完」提前到「首段将播」），后续段
        到达即接力播放。非流式后端退化为单句序列：收集完成 = 首段到达，
        一次播放，行为与改前一致。

        with_send_sound：本地链路在等首段期间播「咻」提示音（pump 已
        先行调度，时序与非流式等价）；原生 ASR 链路无提示音。

        取消语义（打断两层机制不变）：段循环每个句段过 stream_segment
        检查点（取消即不再播剩余段、pump 被 cancel、aclose 关上游 SSE）；
        播放中的段由打断方 stop_tts_playback(令牌) 定向杀流，杀完后的
        下一个检查点静默退出本轮——无「打断后冒半句」。
        """
        queue: asyncio.Queue = asyncio.Queue()
        done = object()
        generator = self.backend.stream_response_segments(text)

        async def _pump():
            try:
                async for segment in generator:
                    await queue.put(segment)
            finally:
                await queue.put(done)

        pump = asyncio.ensure_future(_pump())
        played: list[str] = []
        try:
            if with_send_sound:
                await self._play_send_sound()
            first = True
            while True:
                segment = await queue.get()
                if segment is done:
                    break
                # 检查点（流循环内每个句段）：取消即停播剩余段，静默退出。
                self._checkpoint(CHECKPOINT_STREAM_SEGMENT)
                if first:
                    # 回声防护时点：首段起播前断录（speaking 态 + 物理闭麦）。
                    await self._begin_speaking()
                    # 检查点（播放等待内，起播前）：取消则首段也不起播。
                    self._checkpoint(CHECKPOINT_PLAYBACK)
                    self._playback_token = open_xiaoai_server.begin_playback_session()
                    first = False
                await self._play_segment_tts(segment)
                played.append(segment)
            # 检查点（播放等待内，返回后）：取消则不再播任何后续音频
            # （notify 音等），本轮静默退出。
            self._checkpoint(CHECKPOINT_PLAYBACK)
        finally:
            self._playback_token = None
            if not pump.done():
                pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                # pump 异常按流中断处理：已播段保留，本轮按已有内容收尾。
                logger.error(
                    f"Segment pump failed: {type(exc).__name__}: {exc}",
                    module=self.LOG_MODULE,
                )
            # 显式关流在本协程执行（取消路径下本协程未被 cancel，await
            # 可执行；pump 被 cancel 时 CancelledError 经 async for 传入
            # generator、其 async with 已退出，aclose 幂等兜底）。
            try:
                await generator.aclose()
            except Exception:
                pass
        return played

    async def _play_segment_tts(self, segment: str):
        """播放单个句段（令牌与检查点由 _speak_response_segments 管理）。"""
        try:
            await self.backend._play_response_with_tts(
                segment,
                tts_speaker=self.backend.get_tts_speaker_for_session_key(),
                playback_token=self._playback_token,
            )
        except TurnCancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"TTS playback error: {exc}",
                module=self.LOG_MODULE,
            )
            speaker = get_speaker()
            if speaker:
                await speaker.play(text=segment)

    def _enter_processing(self) -> None:
        """检查点②（LLM 请求前）+ 管线态 → processing（两条 ASR 链路共用）。"""
        self._checkpoint(CHECKPOINT_PRE_LLM)
        conversation_pipeline.set_state(PipelineState.PROCESSING)

    async def _begin_speaking(self) -> None:
        """管线态 → speaking + 物理断录（回声防护，两条链路共用）。"""
        conversation_pipeline.set_state(PipelineState.SPEAKING)
        await self._stop_recording()

    # ---- VAD integration ----

    async def _wait_for_speech(self, vad) -> bytes | None:
        """Use VAD to detect speech and collect complete utterance.

        Follows the same two-step pattern as the XiaoZhi wakeup session:
          1. resume("speech") → wait for speech event (voice detected)
          2. resume("silence") → keep recording → wait for silence event

        Recording taps the VAD frame stream via the public subscription seam
        (P2 ws ticket 04) — speech/silence events arrive through
        subscribe_events, frames through subscribe_frames; both subscriptions
        are scoped to this wait and released in finally (no monkey-patching
        of VAD internals or EventManager callback slots).

        Returns:
            PCM bytes of captured speech, or None on timeout.
        """
        self._vad_future = self._loop.create_future()
        recording_frames: list[bytes] = []
        is_recording = False

        def _on_vad_event(event: str, payload):
            """Voice detected → start recording; silence → resolve.

            在 VAD 检测线程上抛：只做内存操作与线程安全投递。
            """
            nonlocal is_recording
            if event == "speech":
                recording_frames.append(payload)
                is_recording = True
                logger.debug(
                    f"VAD speech detected, buffer size: {len(payload)}",
                    module=self.LOG_MODULE,
                )
                # Now wait for silence to know user stopped speaking
                vad.resume("silence")
            else:
                is_recording = False
                # 「说完」时刻（ticket 02）：TurnTrace 的 speech_end 段锚点；
                # ASR 定稿后由对话循环消费。
                self._speech_end_at = time.monotonic()
                logger.debug(
                    "VAD detected silence, stop recording",
                    module=self.LOG_MODULE,
                )
                if self._vad_future and not self._vad_future.done():
                    self._loop.call_soon_threadsafe(
                        self._vad_future.set_result, b"".join(recording_frames)
                    )

        def _on_vad_frame(kind: str, frames: bytes):
            """录音帧 tap：is_recording 期间两类帧都收（speech+silence）。"""
            if is_recording:
                recording_frames.append(bytes(frames))

        unsubscribe_events = vad.subscribe_events(_on_vad_event)
        unsubscribe_frames = vad.subscribe_frames(_on_vad_frame)
        try:
            vad.resume("speech")
            result = await asyncio.wait_for(self._vad_future, timeout=self.timeout)
            return result

        except asyncio.TimeoutError:
            logger.debug("VAD timeout, no speech detected", module=self.LOG_MODULE)
            vad.pause()
            return None

        finally:
            unsubscribe_events()
            unsubscribe_frames()
            self._vad_future = None

    def _cancel_vad_future(self):
        """Cancel any pending VAD future."""
        if self._vad_future and not self._vad_future.done():
            self._loop.call_soon_threadsafe(self._vad_future.cancel)
        self._vad_future = None

    async def _wait_for_xiaoai_asr_text(self) -> str | None:
        """Wake XiaoAI and wait for a final native ASR result."""
        speaker = get_speaker()
        if not speaker:
            logger.error("Speaker not available", module=self.LOG_MODULE)
            return None

        deadline = self._loop.time() + self.timeout
        while self.active:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None

            self._xiaoai_asr_future = self._loop.create_future()
            try:
                wait_seconds = max(0.1, remaining)
                logger.debug(
                    f"Triggering XiaoAI native ASR, waiting up to {wait_seconds:.1f}s",
                    module=self.LOG_MODULE,
                )
                await speaker.wake_up(awake=True, silent=True)
                result = await asyncio.wait_for(
                    self._xiaoai_asr_future,
                    timeout=wait_seconds,
                )
                if result == self.XIAOAI_ASR_TIMEOUT:
                    logger.debug(
                        "XiaoAI native ASR ended without speech (native timeout), retrying",
                        module=self.LOG_MODULE,
                    )
                    continue
                return result
            except asyncio.TimeoutError:
                logger.info("XiaoAI native ASR hit outer wait timeout", module=self.LOG_MODULE)
                return None
            finally:
                self._xiaoai_asr_future = None

        return None

    def consume_xiaoai_recognize_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Consume XiaoAI native ASR events while the backend is waiting."""
        if not (
            self.active
            and self.uses_xiaoai_asr()
            and self._xiaoai_asr_future
        ):
            return False

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not is_final:
            logger.debug(
                f"Ignoring partial XiaoAI ASR result: {normalized_text}",
                module=self.LOG_MODULE,
            )
            return True

        # Silent wakeup may emit an empty final marker before real speech starts.
        if not normalized_text and is_vad_begin is False:
            logger.debug("Ignoring XiaoAI wake marker for native ASR", module=self.LOG_MODULE)
            return True

        if normalized_text:
            turn = self._pipeline_turn
            logger.debug(
                f"turn={turn.turn_id if turn else '-'} "
                f"XiaoAI native ASR recognized: {normalized_text}",
                module=self.LOG_MODULE,
            )
            # 检查点①（ASR 定稿后，事件线程侧）：本轮已取消则吞掉定稿文本、
            # 不 resolve future（静默；循环协程由打断方 task.cancel 收尾）。
            if turn is not None and not turn.check(CHECKPOINT_ASR_FINAL):
                return True
            # TurnTrace（ticket 11）：is_final 到达即本轮锚点，只记字符数、不记原文。
            # 轮次号与 pipeline 同源（ticket 02）。
            turn_trace.begin_turn(
                "xiaoai_asr",
                asr_chars=len(normalized_text),
                turn_id=turn.turn_id if turn is not None else None,
            )
            self._resolve_xiaoai_asr_future(normalized_text)
            return True

        logger.debug(
            "XiaoAI native ASR received empty final result",
            module=self.LOG_MODULE,
        )
        self._resolve_xiaoai_asr_future(self.XIAOAI_ASR_TIMEOUT)
        return True

    def _resolve_xiaoai_asr_future(self, text: str):
        """Resolve the pending XiaoAI ASR future on the owner loop."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.set_result, text)

    def _cancel_xiaoai_asr_future(self):
        """Cancel any pending XiaoAI ASR future."""
        if self._xiaoai_asr_future and not self._xiaoai_asr_future.done():
            self._loop.call_soon_threadsafe(self._xiaoai_asr_future.cancel)
        self._xiaoai_asr_future = None

    async def _stop_xiaoai_native_listening(self):
        """Exit XiaoAI native listening before playing the goodbye prompt."""
        speaker = get_speaker()
        if not speaker:
            return
        try:
            await speaker.stop_device_audio()
            await speaker.wake_up(awake=False)
            await asyncio.sleep(0.15)
        except Exception as exc:
            logger.debug(
                f"Failed to stop XiaoAI native listening: {exc}",
                module=self.LOG_MODULE,
            )

    async def _wait_for_silence(self, vad):
        """Wait until the environment is silent before starting to listen.

        Uses VAD silence detection to confirm the speaker has stopped playing.
        If speech is detected (e.g. TTS tail), keeps retrying silence detection.
        Events arrive through the public subscription seam (P2 ws ticket 04) —
        scoped to this wait, released in finally.
        """
        future = self._loop.create_future()

        def _on_vad_event(event: str, payload):
            if event == "silence":
                if not future.done():
                    self._loop.call_soon_threadsafe(future.set_result, True)
            else:
                # Still hearing sound — switch back to silence detection
                vad.resume("silence")

        unsubscribe = vad.subscribe_events(_on_vad_event)
        try:
            vad.resume("silence")
            await asyncio.wait_for(future, timeout=1)
        except asyncio.TimeoutError:
            vad.pause()
        finally:
            unsubscribe()

    # ---- Recording control (physical mute/unmute via remote arecord) ----

    async def _stop_recording(self):
        """Kill the remote arecord process so the mic doesn't pick up TTS."""
        try:
            from core.xiaoai import XiaoAI
            await XiaoAI.stop_recording()
            logger.debug("Recording stopped", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"stop_recording error: {exc}", module=self.LOG_MODULE)

    async def _start_recording(self):
        """Restart the remote arecord process to resume mic input."""
        try:
            from core.xiaoai import XiaoAI
            await XiaoAI.start_recording()
            logger.debug("Recording started", module=self.LOG_MODULE)
        except Exception as exc:
            logger.debug(f"start_recording error: {exc}", module=self.LOG_MODULE)

    # ---- TTS ----

    async def _play_notify(self):
        """Play the listening-ready notification sound via PCM buffer."""
        if not _NOTIFY_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_NOTIFY_PCM)
                # Wait for playback to finish: PCM is int16 at 24000Hz
                duration = len(_NOTIFY_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Notify sound error: {exc}", module=self.LOG_MODULE)

    async def _play_send_sound(self):
        """Play the send notification before waiting for backend response."""
        if not _SEND_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_SEND_PCM)
                # 等待播放完成：PCM 为 int16，24000Hz
                duration = len(_SEND_PCM) / (24000 * 2)
                await asyncio.sleep(duration)
            except Exception as exc:
                logger.debug(f"Send sound error: {exc}", module=self.LOG_MODULE)

    async def _call_after_wakeup(self):
        """Call the user-defined after_wakeup hook.

        The hook is user-supplied config; guard it so a bug in the callback
        (e.g. an IndexError while parsing session_key) never tears down the
        conversation exit flow.
        """
        after_wakeup = self.config.get_app_config("wakeup.after_wakeup")
        if after_wakeup:
            speaker = get_speaker()
            if speaker:
                try:
                    await after_wakeup(
                        speaker,
                        source=self.WAKEUP_SOURCE,
                        session_key=self.backend._session_key,
                    )
                except Exception as exc:
                    import traceback
                    logger.error(
                        f"after_wakeup hook error: {type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}",
                        module=self.LOG_MODULE,
                    )
