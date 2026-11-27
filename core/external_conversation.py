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
import sys
import time

import open_xiaoai_server

from core.ref import get_speaker, get_vad
from core.services.audio.playback_timing import sleep_playback_done
from core.services.audio.speaking_channel import SpeakingChannel
from core.services.speaker import AnnounceRejected
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


async def _preempt_cch_silence(source: str) -> bool:
    """CCH 语音轮 ASR 入口预告静音（fire-and-forget pause）。

    真机证据（2026-11-26）：用户说完→external/text 到达（14:24:40.449）到
    LLM tool 决策（14:24:41.842）有 ~1.4s，旧曲在这段继续响。pause 提前到
    ASR 入口（此处）即可按住，后续 tool 的 song_request.silence() 为幂等重复。

    仅 cch 前缀 source 静音（LX06 原生语音不误伤）；问答轮音乐保持暂停
    亦符合用户预期（2026-11-26 裁决）。
    """
    if "cch" not in str(source or "").lower():
        return False
    from core.music import cch_cast

    ok, _ = await cch_cast.host_cmd("pause", wait_ack=False)
    return bool(ok)


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
        # 发声接缝（B-29 步 2）：播放 API 只经 open() 产出的 session 暴露，
        # 「发声前必须物理断录」由结构强制而非注释纪律
        self.speaking_channel = SpeakingChannel(self)
        # 外部文本轮（B-37）：API 层经 WakeupSessionManager.begin_external_text_session
        # 预置后 start()；单轮语义——该轮结束即整个会话退出，不进聆听循环。
        self._external_request: dict | None = None
        # 本轮后端回复段（外部轮 reply 面板/审计用；播报与否按 silent 分流）
        self._external_reply: list[str] = []
        # 轮终结果（turn_id/outcome/reply），finish_external_text_session 消费
        self._external_result: dict | None = None

    # ---- config helpers ----

    def _cfg(self, key: str, default=None):
        return self.config.get_app_config(f"{self.CONFIG_PREFIX}.{key}", default)

    @property
    def exit_keywords(self) -> list[str]:
        # 默认不含「停止」：它是媒体控制词（song_request action=stop），
        # 不能先被退出会话截胡（真机 2026-10-28）。
        return self._cfg("exit_keywords", ["退出", "再见"])

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

    def begin_external_turn(self, text: str, *, source: str = "", silent: bool = False) -> None:
        """预置外部文本轮（B-37）。必须在 start() 之前调用。

        单轮语义：该轮结束即整个会话退出（`_conversation_loop` 外部分支），
        无下一轮聆听；silent=True 时回复只收集不发声（CCH 屏显场景）。
        """
        self._external_request = {
            "text": str(text or ""),
            "source": str(source or ""),
            "silent": bool(silent),
        }
        self._external_reply = []
        self._external_result = None

    @property
    def _external_silent(self) -> bool:
        return bool(self._external_request and self._external_request.get("silent"))

    async def start(self):
        """Enter OpenClaw conversation mode."""
        if self.active:
            logger.warning(f"[{self.LOG_MODULE}] Already active, ignoring start()")
            return
        self.active = True
        self._loop = asyncio.get_running_loop()

        logger.info(f"🎙️ 进入 {self.BACKEND_NAME} 连续对话模式", module=self.LOG_MODULE)

        # 会话级 TTS focus（B-07）：整段连续对话期间保持音乐暂停（回声防护），
        # 对话结束（退出词/超时）后才按资格恢复。点歌起播会直接结束会话。
        # 注：「停止聆听」不在对话内处理（2026-11-01 用户裁决）——对话中 KWS
        # 暂停、免唤醒路径不可达，该词只匹配 exit_keywords 之外的普通文本进
        # LLM；要停止聆听须先退出对话再经 KWS 路径（含二次确认闸门）。
        # 每设备独立 focus（用户裁决②）：CCH 外部轮只按住 CCH，不碰 LX06 音乐；
        # 其它轮次（LX06/native）维持默认 renderer=lx06 原行为。
        _asr_source = str((self._external_request or {}).get("source") or "").lower()
        _renderer = "cch" if "cch" in _asr_source else "lx06"
        focus_held = False
        try:
            from core.music import focus_bridge

            focus_held = await focus_bridge.hold("conversation", renderer=_renderer)
        except Exception as exc:  # noqa: BLE001 - focus 失败不阻断对话
            logger.debug(f"[{self.LOG_MODULE}] TTS focus unavailable: {exc}")
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
            if focus_held:
                try:
                    from core.music import focus_bridge

                    await focus_bridge.release_hold(renderer=_renderer)
                except Exception as exc:  # noqa: BLE001 - 收尾不得抛
                    logger.debug(f"[{self.LOG_MODULE}] TTS focus release failed: {exc}")

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
        if self.uses_xiaoai_asr() and self._external_request is None:
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

        # 外部文本轮（B-37）：不跑序前导（断录/notify/开录）——外部轮不聆听，
        # 预置文本直接进轮模板，单轮后整个会话退出。
        if self._external_request is not None:
            await self._run_external_turn()
            return

        # Mute mic → play notify → unmute.
        # _notify_sound() blocks for ~740ms (the beep duration), during which
        # the mic is off and before_wakeup TTS echo naturally fades.
        # VAD.resume() resets all state (speech_frames, input_bytes),
        # so speech detection starts clean when listening begins.
        await self._stop_recording()
        logger.debug("Recording stopped", module=self.LOG_MODULE)
        await self._notify_sound()
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
                result = await self._run_one_turn()
                if result in ("exit", "timeout", "exit_keep_audio"):
                    outcome = result
                    if self.uses_xiaoai_asr():
                        if result == "exit_keep_audio":
                            # 点歌等工具已接管音频：只结束聆听会话，不停设备播放
                            await self._leave_xiaoai_listening_keep_audio()
                        else:
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

    async def _run_external_turn(self):
        """外部文本单轮（B-37）：预置文本进既有轮模板，轮终即会话退出。

        spec 三待确认点的定案（`ws: .scratch/external-asr-entry/spec.md`）：
          - ⓐ 不聆听：`_conversation_loop` 外部分支跳过序前导，
            `_notify_and_resume_listening` 对外部轮 no-op（无下一轮）。
            但**会话收尾按「进入时在录且已被本轮断录」恢复录音**——
            本轮 TTS 经 speaking_channel 物理断录后若不恢复，KWS resume
            后无帧失聪（看门狗 expected=false 不接管，AGENTS.md 录音通道节）。
          - ⓑ silent 只压回复面（`_speak_response_segments` collect-only），
            护栏交互（C/B 确认提示、无回复降级）照旧播报——护栏对本入口
            的文本视同语音输入，一视同仁。
          - ⓒ 会话起动绕开 `wakeup()`/before_wakeup（无开场白播报副作用），
            由 `WakeupSessionManager.begin_external_text_session` 直入会话。
        """
        req = self._external_request or {}
        was_recording = self._recording_expected()
        turn = conversation_pipeline.begin_turn("external_text")
        self._pipeline_turn = turn
        outcome = "played"
        # T2 ticket 08：按 ASR 来源设本轮默认播放目标（规则兜底层）。
        # CCH 语音（source=cch-screen 等 cch 前缀）默认投 CCH；其余维持 lx06。
        # LLM 可在 song_request  tool 的 target 参数里显式覆盖。
        from core.tools.song_request import set_playback_target

        _asr_source = str(req.get("source") or "").lower()
        set_playback_target("cch" if "cch" in _asr_source else "lx06",
                            text=str(req.get("text") or ""))
        # ASR 入口预告静音：覆盖 ASR→LLM tool 决策窗口（真机 ~1.4s 旧曲残留）。
        # fire-and-forget，不阻断对话；后续点播 tool 的 silence 为幂等重复。
        await _preempt_cch_silence(_asr_source)
        try:
            result = await self._run_one_turn()
            # 单轮语义：continue 也结束会话（外部轮无下一轮）。
            outcome = "played" if result == "continue" else str(result)
        except TurnCancelledError as exc:
            outcome = f"cancelled:{exc.checkpoint}"
            logger.info(
                f"turn={exc.turn_id} external turn silent exit at checkpoint={exc.checkpoint} "
                f"reason={exc.reason}",
                module=self.LOG_MODULE,
            )
        finally:
            # T2 ticket 08：轮末清除本轮播放目标兜底，避免污染后续轮。
            from core.tools.song_request import reset_playback_target

            reset_playback_target()
            if outcome == "played" and turn is not None and turn.cancelled:
                outcome = "cancelled"
            self._pipeline_turn = None
            conversation_pipeline.end_turn(outcome, turn=turn)
            self._external_result = {
                "turn_id": turn.turn_id if turn is not None else None,
                "outcome": outcome,
                "reply": "".join(self._external_reply),
            }
            if was_recording and not self._recording_expected():
                # 本轮发声断录：恢复录音通道，KWS resume 后才有帧可听。
                await self._start_recording()

    @staticmethod
    def _recording_expected() -> bool:
        """录音通道当前是否应持续（XiaoAI 公开只读快照，外部轮收尾恢复判据）。"""
        from core.xiaoai import XiaoAI

        return XiaoAI.recording_expected()

    def external_result(self) -> dict | None:
        """外部轮结果（turn_id/outcome/reply）；轮未收尾或非外部轮时 None。

        `finish_external_text_session` 经此取结果——不直访 `_external_result`。
        """
        return self._external_result

    async def _run_one_turn(self) -> str:
        """单轮固定编排（B-28 票 03 轮模板，收敛自双写的 _run_one_turn_with_*）：

        取文本 → 取消检查点① → C/B 应答拦截 → exit 关键词 → 媒体命令
        → 检查点②/processing → 句段流播报 → 无回复降级播报 → notify → 开录
        （→ VAD 链路等静音）。

        输入链路差异只存在于 _acquire_user_text()（策略点）；编排语义改动一律
        落在本方法单点，不再双写。返回值契约：
            "continue" - turn completed, loop to next
            "exit"     - user said an exit keyword
            "timeout"  - no speech detected within timeout
            "error"    - unrecoverable error
        """
        status, text = await self._acquire_user_text()
        if status != "ok":
            return status  # "timeout" / "continue"（本地 ASR 空结果重试）/ "error"

        # 取消检查点①（ASR 定稿后）：本轮已取消则静默退出（不发 LLM 请求）。
        # 小爱链路已在 consume 回调（事件线程）非抛式过一次，这里再兜底一次。
        self._checkpoint(CHECKPOINT_ASR_FINAL)

        # B-37 补（C-6 执行面）：外部轮指令表路由。外部会话直入（spec ⓒ 绕开
        # wakeup()/before_wakeup），DIRECT_COMMANDS 的 KWS 词表路由不在链上，
        # 表命中文本会漏进 LLM 当聊天。此处对齐唤醒路径语义（router 的
        # source=="kws" 分支）：resolve_command_key 命中即 run_steps 执行、
        # 不发 LLM。仅外部轮生效——语音路径的表匹配仍在 route_before_wakeup。
        if self._external_request is not None:
            from core.commands.executor import run_steps
            from core.commands.matching import resolve_command_key
            from core.utils.config_loader import ensure_config_module_loaded

            # 不用裸 sys.modules.get("config")：ConfigManager 的 loader 会以
            # __file__ 校验为由整模块替换该条目，测试桩会被静默冲掉；
            # ensure 语义 = 保证真实配置模块在位并返回它（与生产一致）。
            config_mod = ensure_config_module_loaded()
            direct_table = (getattr(config_mod, "DIRECT_COMMANDS", None) or {}) if config_mod else {}
            aliases = getattr(config_mod, "COMMAND_ALIASES", None) if config_mod else None
            steps_key = resolve_command_key(text, direct_table, aliases)
            if steps_key is not None:
                # 无 LLM/TTS 句段：TurnTrace 立即收尾为 incomplete（拦截轮口径，
                # 同 confirmation intercept / media command）。
                turn_trace.abandon_turn()
                logger.info(
                    f"[ExternalText] direct command hit: key={steps_key}",
                    module=self.LOG_MODULE,
                )
                speaker = get_speaker()
                if speaker is not None:
                    # 步骤序列里的 str（TTS）仍走 announce 闸门照常发声——
                    # silent 只压 LLM 回复面（spec ⓑ），指令表自身的播报语义不变。
                    await run_steps(speaker, direct_table[steps_key])
                return "exit"

        # C/B 级写操作应答拦截（P3 票 04）：在 exit 关键词之前——「取消」
        # 也是合法应答，不能先被退出关键词截胡。
        confirm_reply = await self._try_handle_confirmation(text)
        if confirm_reply is not None:
            # TurnTrace（ticket 11）：拦截应答没有 LLM/TTS 句段，立即收尾为 incomplete。
            turn_trace.abandon_turn()
            await self._begin_speaking()
            await self._play_bypass_text(confirm_reply, reason="confirmation intercept")
            await self._notify_and_resume_listening()
            return "continue"

        # 3. Check exit keywords
        for kw in self.exit_keywords:
            if kw in text:
                logger.info(f"Exit keyword: {kw}", module=self.LOG_MODULE)
                turn_trace.abandon_turn()
                # v0.4.6：退出时同步停 CCH 播放并清宿主自愈状态——「退下」的合法停止
                # 不能被 M5 self-heal 当冻结重新拉起（2026-11-21 真机：退下后歌曲被
                # self-heal 反复拉回，反复多次无法退出）。
                try:
                    from core.music.cch_cast import host_cmd
                    await host_cmd("stop")
                except Exception as exc:
                    logger.warning(f"Exit stop CCH playback failed: {exc}", module=self.LOG_MODULE)
                return "exit"

        # 3.4 对话轮内媒体命令：「继续播放/暂停/停止播放/下一首/上一首」
        # 在 exit 之后、LLM 之前——媒体命令绝不发给模型当聊天（真机 2026-10-30：
        # 对话态裸说「继续播放」无反应）；无音乐时照常走 LLM，不吞普通对话。
        media_result = await self._try_handle_media_command(text)
        if media_result is not None:
            return media_result

        # 3.5 「换个话题」暗号（spec topic-switch-and-memory 票 03）：exit 之后、
        # processing 之前——命中则后端原子完成「清历史 + 置信令 + 播过渡语」，
        # 不发 LLM。基类默认不识别（仅 OpenAI 路径接入，见 openai_conversation）。
        if await self._maybe_handle_new_topic(text):
            return "continue"

        # 检查点②（LLM 请求前）+ 管线态 → processing
        # 每轮复位「工具接管音频」标记：song_request 起播成功会置位，
        # 本轮收尾 TTS 跳过（否则同设备 TTS 会打断刚起播的音乐）。
        from core.tools.registry import reset_reply_suppression

        reset_reply_suppression()
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
        #    票 05：发模型的是「原始 + 纪律 prompt」增强文本，落历史/记忆的
        #    须是原始 ASR 文本——发请求前登记、收尾（含异常）清除。
        self.backend.note_raw_turn_text(text)
        try:
            full_text = self._with_rule_prompt(text)
            segments = await self._speak_response_segments(
                full_text, with_send_sound=not self.uses_xiaoai_asr()
            )
        finally:
            self.backend.note_raw_turn_text("")
        if not segments:
            from core.tools.registry import reply_suppressed

            if reply_suppressed():
                # 工具已自行播报并起播（音频已交给工具）：不播降级语，退出会话保音频
                return "exit_keep_audio"
            logger.warning(f"No response from {self.BACKEND_NAME}", module=self.LOG_MODULE)
            await self._play_bypass_text("抱歉，我没有收到回复", reason="no response fallback")
            return "continue"

        # 5. Notify → Start recording →（VAD 链路再）Wait for silence
        from core.tools.registry import reply_suppressed

        if reply_suppressed():
            # 工具已接管音频（音乐在播）：退出连续对话、保留设备播放。
            # 真机 2026-10-27：会话态下设备原生助手会把流式播放抢回并停掉
            # （采样显示起播 ~10s 后 stat=1→2，随后设备自报网络异常）。
            return "exit_keep_audio"
        await self._notify_and_resume_listening()
        return "continue"

    async def _maybe_handle_new_topic(self, text: str) -> bool:
        """换话题暗号钩子（B-30 票 03）：默认不识别；OpenAI 控制器覆写。"""
        return False

    async def _try_handle_media_command(self, text: str) -> str | None:
        """对话轮内媒体命令：裸说「继续播放/暂停/停止播放/下一首/上一首」。

        真机 2026-10-30：连续对话期间 KWS 已暂停，裸说的媒体命令只会被当成
        聊天发给 LLM（口头答应、设备不动）。这里在 ASR 定稿后、发请求前直接
        执行（`song_request` 的 action 分发），绝不把这类文本交给模型。

        只在媒体相关状态拦截（`core.commands.media.media_active`：托管会话在管
        / 旧队列有当前曲，含暂停态）——没有音乐时「继续播放」照常走 LLM。

        返回值：
            None              - 未命中媒体命令或当前无媒体（本轮照常发 LLM）
            "continue"        - 已执行且音乐仍静默（暂停）：提示音后回到聆听
            "exit_keep_audio" - 已执行且音频所有权已交接（继续/切歌/停止）：
                                单轮收束会话，不停设备播放
        """
        from core.commands.media import (
            execute_media_command,
            hands_over_audio,
            match_media_command,
            media_active,
        )

        action = match_media_command(text)
        if action is None or not media_active():
            return None

        # 本轮没有 LLM/TTS 句段：TurnTrace 立即收尾为 incomplete（拦截轮口径）。
        turn_trace.abandon_turn()
        result = await execute_media_command(action)
        logger.info(
            f"media command in turn: action={action} result={result!r}",
            module=self.LOG_MODULE,
        )
        if hands_over_audio(action):
            return "exit_keep_audio"
        # 暂停不交接音频：音乐保持静默，提示音后继续听下一轮（多轮语义不变）
        await self._begin_speaking()
        await self._notify_and_resume_listening()
        return "continue"

    def _with_rule_prompt(self, text: str) -> str:
        """拼连续对话轮的输出纪律 prompt（走公开访问器，不直访私有属性）。"""
        rule_prompt = self.backend.current_rule_prompt()
        return f"{text}\n{rule_prompt}" if rule_prompt else text

    async def _notify_and_resume_listening(self):
        """提示音 → 开录 →（VAD 链路）等静音。mic 已由 _speak_response_segments
        在首段前闭掉；notify 阻塞 ~740ms（提示音时长）足够 TTS 回声衰减；
        开录后等静音确保残留回声/缓冲清空，VAD.resume() 已重置全部状态，
        语音检测从干净状态开始。"""
        # B-37 外部轮：单轮语义，无下一轮聆听——提示音/开录/等静音全跳过，
        # 录音通道由 _run_external_turn 收尾按需恢复。
        if self._external_request is not None:
            return
        await self._notify_sound()
        await self._start_recording()
        if not self.uses_xiaoai_asr():
            vad = get_vad()
            if vad:
                logger.debug("Recording started, waiting for silence...", module=self.LOG_MODULE)
                await self._wait_for_silence(vad)
        logger.debug("Ready for next round", module=self.LOG_MODULE)

    async def _acquire_user_text(self) -> tuple[str, str | None]:
        """取本轮用户文本——输入链路的唯一策略点（B-28 票 03）。

        返回 (status, text)：
            ("ok", text)      拿到本轮文本
            ("timeout", None) 本地链路无语音 / 原生 ASR 超时
            ("continue", None) 本地 ASR 空结果（重试，不结束会话）
            ("error", None)   本地链路 VAD 不可用
        """
        # 外部预置文本（B-37）：优先于两条 ASR 链路——外部轮不吃麦克风输入，
        # 文本已由调用方（CCH 端截获 / PC 工具）识别完成。
        if self._external_request is not None:
            text = str(self._external_request.get("text") or "")
            caller = str(self._external_request.get("source") or "")
            turn = self._pipeline_turn
            logger.info(
                f"[ExternalText] turn={turn.turn_id if turn else '-'} "
                f"caller={caller or 'unknown'} len={len(text)} "
                f"silent={self._external_silent}",
                module=self.LOG_MODULE,
            )
            trace = turn_trace.begin_turn(
                "external_text",
                asr_chars=len(text),
                turn_id=turn.turn_id if turn else None,
                caller=caller,
            )
            if trace is not None:
                trace.mark("asr_final")
            return "ok", text

        if self.uses_xiaoai_asr():
            text = await self._wait_for_xiaoai_asr_text()
            if text is None:
                logger.debug("XiaoAI native ASR turn timed out", module=self.LOG_MODULE)
                return "timeout", None
            return "ok", text

        vad = get_vad()
        if not vad:
            logger.error("VAD not available", module=self.LOG_MODULE)
            return "error", None

        # 1. Start listening for speech (recording is already active)
        speech_bytes = await self._wait_for_speech(vad)
        if speech_bytes is None:
            return "timeout", None

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
            return "continue", None

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
        return "ok", text

    # ---- 管线态与取消传播（ticket 02）----

    def _checkpoint(self, name: str) -> None:
        """异步检查点：当前轮已取消时抛 TurnCancelledError（无轮时直通）。"""
        turn = self._pipeline_turn
        if turn is not None:
            turn.checkpoint(name)

    async def _try_handle_confirmation(self, text: str) -> str | None:
        """C/B 级写操作的应答拦截（P3 票 04 对话层接线）。

        ASR 定稿后、exit 关键词前调用：有等待确认的写操作且用户话语是
        确认/否认 ⇒ 执行或放弃，返回播报文案（announce 后本轮不走 LLM）；
        否则返回 None（本轮照常）。
        """
        try:
            from core.tools.guardrails import handle_confirmation_utterance

            return await handle_confirmation_utterance(text)
        except Exception as exc:  # noqa: BLE001 - 护栏拦截绝不杀对话
            logger.error(
                f"confirmation intercept failed: {type(exc).__name__}: {exc}",
                module=self.LOG_MODULE,
            )
            return None

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

        B-37 外部轮：`_external_silent` 时 collect-only——段只收集不发声
        （不开 channel、不播咻、不断录），回复文本经 _external_reply 交给
        端点响应；非 silent 外部轮照常走既有 TTS 出口。

        取消语义（打断两层机制不变）：段循环每个句段过 stream_segment
        检查点（取消即不再播剩余段、pump 被 cancel、aclose 关上游 SSE）；
        播放中的段由打断方 stop_tts_playback(令牌) 定向杀流，杀完后的
        下一个检查点静默退出本轮——无「打断后冒半句」。
        """
        queue: asyncio.Queue = asyncio.Queue()
        done = object()
        generator = self.backend.stream_response_segments(text)
        collect_only = self._external_silent

        # 入口复位（与 _run_one_turn 的轮首复位双保险）：工具置位发生在本
        # 方法内部（pump 驱动 LLM 期间），此处清的是上一轮可能的残留。
        from core.tools.registry import reset_reply_suppression

        reset_reply_suppression()

        async def _pump():
            try:
                async for segment in generator:
                    await queue.put(segment)
            finally:
                await queue.put(done)

        pump = asyncio.ensure_future(_pump())
        played: list[str] = []
        channel_ctx = None
        try:
            if with_send_sound and not collect_only:
                # 「咻」在断录前播（既有设计）：保 LLM 等待期 KWS/打断可用。
                await self._play_send_sound()
            first = True
            session = None
            muted = False
            while True:
                segment = await queue.get()
                if segment is done:
                    break
                # 外部轮回复收集（silent 与否都收——reply 是内容事实，
                # 播报与否是出口策略）
                if self._external_request is not None:
                    self._external_reply.append(segment)
                # 检查点（流循环内每个句段）：取消即停播剩余段，静默退出。
                self._checkpoint(CHECKPOINT_STREAM_SEGMENT)
                if collect_only:
                    played.append(segment)
                    continue
                if first and not muted:
                    from core.tools.registry import reply_suppressed

                    if reply_suppressed():
                        # 工具本轮已自行播报并起播（song_request）：收尾文本只消费
                        # 不发声，避免同设备 TTS 把刚开始的音乐顶掉。
                        muted = True
                        logger.info(
                            "reply TTS suppressed: tool took over audio this turn",
                            module=self.LOG_MODULE,
                        )
                if muted:
                    played.append(segment)
                    continue
                if first:
                    # 回声防护时点：首段起播前断录（speaking 态 + 物理闭麦）。
                    # B-29 步 2：open() 进门即断录，播放 API 只在 session 上；
                    # open 惰性发生在此（非循环前）——LLM 等待期录音不停，可打断。
                    channel_ctx = self.speaking_channel.open(reason="response segments")
                    session = await channel_ctx.__aenter__()
                    conversation_pipeline.set_state(PipelineState.SPEAKING)
                    # 检查点（播放等待内，起播前）：取消则首段也不起播。
                    self._checkpoint(CHECKPOINT_PLAYBACK)
                    self._playback_token = open_xiaoai_server.begin_playback_session()
                    first = False
                await session.play_segment_tts(segment)
                played.append(segment)
            # 检查点（播放等待内，返回后）：取消则不再播任何后续音频
            # （notify 音等），本轮静默退出。
            self._checkpoint(CHECKPOINT_PLAYBACK)
        finally:
            if channel_ctx is not None:
                await channel_ctx.__aexit__(*sys.exc_info())
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

    # _play_segment_tts 已迁入 SpeakingSession.play_segment_tts（B-29 步 2：
    # 播放 API 只经 speaking_channel.open() 产出的 session 暴露）。

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
                wake_res = await speaker.wake_up(awake=True, silent=True)
                # 🔍 ASR 会话诊断：wake 失败会被吞成对话聋，升 INFO 留证
                logger.info(
                    f"ASR kick wake_up(awake=True,silent) -> {wake_res}",
                    module=self.LOG_MODULE,
                )
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

    async def _leave_xiaoai_listening_keep_audio(self):
        """结束原生聆听会话但不停设备播放（点歌接管音频的退出路径）。

        与 _stop_xiaoai_native_listening 的唯一差别：不发 stop_device_audio。
        会话太久会让设备原生助手抢回流式播放并停掉（真机 2026-10-27）。
        """
        speaker = get_speaker()
        if not speaker:
            return
        try:
            await speaker.wake_up(awake=False)
            await asyncio.sleep(0.15)
        except Exception as exc:
            logger.debug(
                f"Failed to leave XiaoAI listening (keep audio): {exc}",
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

    async def _notify_sound(self):
        """notify 提示音（B-29 步 2 经 SpeakingChannel）：复用调用方已完成的
        断录窗口（句段播完后 / start 入口显式断录后），不重复断录 RPC。"""
        async with self.speaking_channel.open(reason="notify sound", stop_recording=False) as session:
            await session.play_pcm(_NOTIFY_PCM, reason="Notify")

    async def _play_bypass_text(self, text: str, *, reason: str) -> None:
        """对话内旁路文本播报（B-29 步 3，评审 S4 方案 B 步 2 的门控版）。

        默认裸 play（现状口径）；`conversation.bypass_play_via_announce=true`
        时经 announce（mic_gate + KWS 闸门）——三处旁路（无回复降级 / C 级
        确认应答 / TTS 失败 fallback）目前绕过全部闸门，其中降级播报发生在
        录音未停的窗口，mic_gate 是唯一的机械防护。词表拒绝留 WARNING 不播。
        默认关：真机 A/B（三轮对话轮间隙比对）后再转正。
        """
        speaker = get_speaker()
        if not speaker:
            return
        if self.config.get_app_config("conversation.bypass_play_via_announce", False):
            try:
                async with speaker.announce(
                    text=text, reason=reason, wait=False, gate_seconds=30
                ):
                    await speaker.play(text=text)
            except AnnounceRejected as exc:
                logger.warning(
                    f"bypass announce rejected: {exc}",
                    module=self.LOG_MODULE,
                )
                return
            return
        await speaker.play(text=text)

    async def _play_send_sound(self):
        """Play the send notification before waiting for backend response."""
        if not _SEND_PCM:
            return
        speaker = get_speaker()
        if speaker:
            try:
                await speaker.play(buffer=_SEND_PCM)
                await sleep_playback_done(_SEND_PCM)
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
                        session_key=self.backend.current_session_key(),
                    )
                except Exception as exc:
                    import traceback
                    logger.error(
                        f"after_wakeup hook error: {type(exc).__name__}: {exc}\n"
                        f"{traceback.format_exc()}",
                        module=self.LOG_MODULE,
                    )
