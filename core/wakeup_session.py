import asyncio
import time

from core.ref import (
    get_app,
    get_kws,
    get_speaker,
    get_xiaozhi,
)
from core.services.protocols.typing import AbortReason
from core.utils.config import ConfigManager
from core.utils.logger import logger


class WakeupSessionManager:
    """Dispatches wakeup events to XiaoZhi or external backend controllers."""

    # ---- 注入窗口（firmware-asr-injection-feasibility.md §3.4 机制 A：闸门）----
    # PC 经 LD_PRELOAD 向固件 ASR 链路注入 PCM 时，bridge 必须吞掉随之而来的
    # RecognizeResult（否则注入内容会被当作用户指令交给 Agent / 对话后端）。
    # 闸门带 TTL 自恢复：PC 崩溃/断网未发关闭请求时，到期自动失效，不会永久卡死。
    INJECTION_GATE_TTL = 30.0  # 秒；PC 侧每次注入前需重新开闸
    _injection_gate_until: float = 0.0  # unix 时间戳，闸门有效期至

    def __init__(self):
        self.config = ConfigManager.instance()
        self._openclaw_controller = None
        self._openclaw_task: asyncio.Task | None = None
        self._openai_controller = None
        self._openai_task: asyncio.Task | None = None
        self._qwenpaw_controller = None
        self._qwenpaw_task: asyncio.Task | None = None
        self._xiaozhi_future: asyncio.Future | None = None

    def _get_loop(self):
        app = get_app()
        if app:
            return app.loop
        from core.xiaoai import XiaoAI
        return XiaoAI.async_loop

    async def _stop_device_playback(self):
        """Stop all audio playback on the device and restart recording.

        - killall tts_play.sh miplayer: stop blocking TTS (tts_play.sh + child miplayer)
        - mphelper pause: stop non-blocking TTS (mibrain text_to_speech via mediaplayer)
        - stop_playing: kill aplay (our PCM channel)
        - start_playing / start_recording: restart audio streams
        """
        speaker = get_speaker()
        if speaker:
            await speaker.stop_device_audio()
            import open_xiaoai_server
            await open_xiaoai_server.start_recording()
            return

        import open_xiaoai_server
        await open_xiaoai_server.stop_playing()
        await open_xiaoai_server.start_recording()

    def on_interrupt(self):
        logger.info("[Wakeup] XiaoAI wakeup — interrupting active sessions")

        loop = self._get_loop()

        # Stop XiaoZhi wakeup session (cancels futures + aborts server audio)
        if self._xiaozhi_future and not self._xiaozhi_future.done():
            self._xiaozhi_future.cancel()
        self._xiaozhi_future = None
        xiaozhi = get_xiaozhi()
        if xiaozhi:
            xiaozhi.stop_wakeup_session()

        # Stop external backend conversations (cancels VAD + stops TTS stream + kills aplay)
        if self._openclaw_controller and self._openclaw_controller.is_active():
            self._openclaw_controller.stop()
        if self._openclaw_task and not self._openclaw_task.done():
            loop.call_soon_threadsafe(self._openclaw_task.cancel)
        if self._openai_controller and self._openai_controller.is_active():
            self._openai_controller.stop()
        if self._openai_task and not self._openai_task.done():
            loop.call_soon_threadsafe(self._openai_task.cancel)
        if self._qwenpaw_controller and self._qwenpaw_controller.is_active():
            self._qwenpaw_controller.stop()
        if self._qwenpaw_task and not self._qwenpaw_task.done():
            loop.call_soon_threadsafe(self._qwenpaw_task.cancel)

        asyncio.run_coroutine_threadsafe(self._stop_device_playback(), loop)

        from core.xiaoai import XiaoAI
        XiaoAI.stop_conversation()

    def on_wakeup(self):
        logger.info("[Wakeup] Wakeup session started")
        xiaozhi = get_xiaozhi()
        if xiaozhi:
            xiaozhi._is_first_round = True
            future = asyncio.run_coroutine_threadsafe(
                xiaozhi.start_wakeup_session(), self._get_loop()
            )
            self._xiaozhi_future = future

            def _clear_future(done_future):
                if self._xiaozhi_future is done_future:
                    self._xiaozhi_future = None

            future.add_done_callback(_clear_future)

    def on_speech(self, speech_buffer: bytes):
        """Called by VAD when speech is detected."""
        pass

    def on_silence(self):
        """Called by VAD when silence is detected."""
        pass

    # ---- 注入闸门（机制 A）----

    @classmethod
    def open_injection_gate(cls, duration: float | None = None) -> float:
        """开启注入窗口：bridge 在窗口内吞掉原生 ASR 结果。

        Returns: 闸门实际有效期（秒）。duration<=0 立即关闸。
        """
        ttl = cls.INJECTION_GATE_TTL if duration is None else float(duration)
        cls._injection_gate_until = time.time() + ttl if ttl > 0 else 0.0
        logger.info(
            f"[Injection] 闸门开启 {ttl:.0f}s（注入期间 RecognizeResult 将被丢弃）"
        )
        return ttl

    @classmethod
    def close_injection_gate(cls) -> None:
        """关闭注入窗口（PC 侧 finally 应显式调用；超时 TTL 也会自恢复）。"""
        if cls._injection_gate_until:
            cls._injection_gate_until = 0.0
            logger.info("[Injection] 闸门关闭")

    @classmethod
    def injection_gate_active(cls) -> bool:
        """闸门是否有效（含 TTL 到期惰性失效）。"""
        if cls._injection_gate_until and time.time() >= cls._injection_gate_until:
            cls._injection_gate_until = 0.0
            logger.warning("[Injection] 闸门 TTL 到期自动恢复（PC 侧可能未正常关闸）")
            # 数据面同步关窗（避免循环依赖，延迟导入）
            try:
                from core.services.api_server import get_relay
                relay = get_relay()
                if relay:
                    relay.close_session("gate TTL expiry")
            except Exception:
                pass
            return False
        return cls._injection_gate_until > 0.0

    @classmethod
    def injection_gate_remaining(cls) -> float:
        """闸门剩余秒数（观测用，0 = 未开）。"""
        return max(0.0, cls._injection_gate_until - time.time()) if cls._injection_gate_until else 0.0

    def consume_xiaoai_asr_result(
        self,
        dialog_id: str,
        text: str,
        is_final,
        is_vad_begin,
    ) -> bool:
        """Route XiaoAI native ASR results to the active external backend controller."""
        for controller in (
            self._openclaw_controller,
            self._openai_controller,
            self._qwenpaw_controller,
        ):
            if controller and controller.is_active():
                return controller.consume_xiaoai_recognize_result(
                    dialog_id=dialog_id,
                    text=text,
                    is_final=is_final,
                    is_vad_begin=is_vad_begin,
                )
        return False

    async def wakeup(self, text, source):
        before_wakeup = self.config.get_app_config("wakeup.before_wakeup")
        kws = get_kws()
        logger.debug(f"[Wakeup] Received wakeup request from {source}: {text}")

        # Reset session_key to config default before each wakeup,
        # so paths that don't call set_openclaw_session_key() always use the default.
        from core.openclaw import OpenClawManager
        default_session_key = self.config.get_app_config("openclaw", {}).get(
            "session_key", "agent:main:open-xiaoai-bridge"
        )
        OpenClawManager._session_key = default_session_key
        from core.openai import OpenAIManager
        default_openai_session_key = self.config.get_app_config(
            "openai", {}
        ).get("session_key", "agent:default:open-xiaoai-bridge")
        OpenAIManager._session_key = default_openai_session_key
        from core.qwenpaw import QwenPawManager
        default_qwenpaw_session_key = self.config.get_app_config(
            "qwenpaw", {}
        ).get("session_key", "agent:default:open-xiaoai-bridge")
        QwenPawManager._session_key = default_qwenpaw_session_key

        if kws:
            kws.pause()
        should_wakeup = await before_wakeup(
            get_speaker(),
            text,
            source,
            get_app(),
        )
        if kws:
            kws.resume()
        logger.info(f"[Wakeup] before_wakeup returned: {should_wakeup}")
        if should_wakeup is not None:
            await self.reset_all_sessions()

        if should_wakeup == "openclaw":
            await self._start_openclaw_conversation()
        elif should_wakeup == "openai":
            await self._start_openai_conversation()
        elif should_wakeup == "qwenpaw":
            await self._start_qwenpaw_conversation()
        elif should_wakeup == "xiaozhi":
            self.on_wakeup()

    async def _start_openclaw_conversation(self):
        """Start an OpenClaw continuous conversation session.

        This runs independently of the XiaoZhi session state machine.
        KWS is paused during the conversation and resumed when done.
        """
        from core.openclaw_conversation import OpenClawConversationController

        kws = get_kws()
        if kws:
            kws.pause()
        try:
            self._openclaw_controller = OpenClawConversationController()
            self._openclaw_task = asyncio.create_task(self._openclaw_controller.start())
            await self._openclaw_task
        except asyncio.CancelledError:
            pass  # interrupted cleanly by on_interrupt
        except Exception as exc:
            logger.error(
                f"[Wakeup] OpenClaw conversation failed: {type(exc).__name__}: {exc}",
                module="Wakeup",
            )
        finally:
            self._openclaw_controller = None
            self._openclaw_task = None
            if kws:
                kws.resume()

    async def _start_openai_conversation(self):
        """Start an OpenAI-compatible continuous conversation session."""
        from core.openai_conversation import OpenAIConversationController

        kws = get_kws()
        if kws:
            kws.pause()
        try:
            self._openai_controller = OpenAIConversationController()
            self._openai_task = asyncio.create_task(self._openai_controller.start())
            await self._openai_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                f"[Wakeup] OpenAI conversation failed: {type(exc).__name__}: {exc}",
                module="Wakeup",
            )
        finally:
            self._openai_controller = None
            self._openai_task = None
            if kws:
                kws.resume()

    async def _start_qwenpaw_conversation(self):
        """Start a QwenPaw continuous conversation session."""
        from core.qwenpaw_conversation import QwenPawConversationController

        kws = get_kws()
        if kws:
            kws.pause()
        try:
            self._qwenpaw_controller = QwenPawConversationController()
            self._qwenpaw_task = asyncio.create_task(self._qwenpaw_controller.start())
            await self._qwenpaw_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error(
                f"[Wakeup] QwenPaw conversation failed: {type(exc).__name__}: {exc}",
                module="Wakeup",
            )
        finally:
            self._qwenpaw_controller = None
            self._qwenpaw_task = None
            if kws:
                kws.resume()

    async def reset_all_sessions(self):
        """Reset all active sessions before starting a new one.

        Stops XiaoAI continuous conversation, interrupts any active XiaoZhi
        session, and stops any external backend continuous conversation.
        """
        from core.xiaoai import XiaoAI
        from core.ref import get_xiaozhi

        # Stop XiaoAI continuous conversation
        XiaoAI.stop_conversation()

        # Interrupt active XiaoZhi session
        xiaozhi = get_xiaozhi()
        if xiaozhi and xiaozhi.is_connected():
            try:
                await xiaozhi.send_abort_speaking(AbortReason.ABORT)
            except Exception:
                pass

        # Stop OpenClaw continuous conversation (also stops its TTS stream)
        if self._openclaw_controller and self._openclaw_controller.is_active():
            self._openclaw_controller.stop()
        if self._openai_controller and self._openai_controller.is_active():
            self._openai_controller.stop()
        if self._qwenpaw_controller and self._qwenpaw_controller.is_active():
            self._qwenpaw_controller.stop()

        # Stop all audio playback on the device
        await self._stop_device_playback()

        logger.debug("[Wakeup] All sessions reset")


EventManager = WakeupSessionManager()
