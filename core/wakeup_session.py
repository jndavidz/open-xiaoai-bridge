import asyncio
import time

from core.backend_registry import backend_registry
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
        # 连续对话会话，按注册表后端名索引（旧版为每后端一对属性）
        self._controllers: dict[str, object] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._xiaozhi_future: asyncio.Future | None = None

    def _get_loop(self):
        app = get_app()
        if app:
            return app.loop
        from core.xiaoai import XiaoAI
        return XiaoAI.async_loop

    async def _stop_device_playback(self):
        """Stop all audio playback on the device and restart recording.

        - 停阻塞 TTS（原厂脚本及其子播放器）
        - 暂停非阻塞 TTS（mibrain 经媒体播放器发声）
        - stop_playing: kill aplay (our PCM channel)
        - start_playing / start_recording: restart audio streams
        """
        speaker = get_speaker()
        if speaker:
            await speaker.stop_device_audio()
            from core.xiaoai import XiaoAI
            await XiaoAI.start_recording()
            return

        import open_xiaoai_server
        await open_xiaoai_server.stop_playing()
        from core.xiaoai import XiaoAI
        await XiaoAI.start_recording()

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
        for name in backend_registry.names():
            controller = self._controllers.get(name)
            if controller and controller.is_active():
                controller.stop()
            task = self._tasks.get(name)
            if task and not task.done():
                loop.call_soon_threadsafe(task.cancel)

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
        for name in backend_registry.names():
            controller = self._controllers.get(name)
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

        # Reset session keys to config defaults before each wakeup, so paths
        # that don't call app.set_<backend>_session_key() always use the
        # default. Goes through the registry public interface — no direct
        # writes to manager private attributes.
        backend_registry.reset_session_keys(self.config)

        if kws:
            kws.pause()
        # P0 ticket 03：before_wakeup 里的播报经 announce 后有了确定性异常路径
        # （AnnounceRejected）；resume 必须放 finally，否则 KWS 会话暂停态
        # （内存标志，无看门狗）会保持到重启，音箱再也听不到唤醒词。
        try:
            should_wakeup = await before_wakeup(
                get_speaker(),
                text,
                source,
                get_app(),
            )
        finally:
            if kws:
                kws.resume()
        logger.info(f"[Wakeup] before_wakeup returned: {should_wakeup}")
        if should_wakeup is not None:
            await self.reset_all_sessions()

        if should_wakeup == "xiaozhi":
            self.on_wakeup()
        elif backend_registry.get(should_wakeup) is not None:
            await self._start_backend_conversation(should_wakeup)

    async def _start_backend_conversation(self, name: str):
        """Start a continuous conversation session for a registry backend.

        通用实现：查注册表取控制器壳（收敛自旧版 _start_<name>_conversation
        三连抄）。会话独立于 XiaoZhi 会话状态机运行；KWS 在对话期间暂停，
        结束（含被打断）后恢复。
        """
        entry = backend_registry.get(name)
        if entry is None:
            logger.warning(f"[Wakeup] Unknown backend for conversation: {name}")
            return

        kws = get_kws()
        if kws:
            kws.pause()
        try:
            controller = entry.load_controller()()
            self._controllers[name] = controller
            task = asyncio.create_task(controller.start())
            self._tasks[name] = task
            await task
        except asyncio.CancelledError:
            pass  # interrupted cleanly by on_interrupt
        except Exception as exc:
            logger.error(
                f"[Wakeup] {entry.display_name} conversation failed: {type(exc).__name__}: {exc}",
                module="Wakeup",
            )
        finally:
            self._controllers.pop(name, None)
            self._tasks.pop(name, None)
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

        # Stop external backend continuous conversations (also stops their TTS streams)
        for controller in tuple(self._controllers.values()):
            if controller.is_active():
                controller.stop()

        # Stop all audio playback on the device
        await self._stop_device_playback()

        logger.debug("[Wakeup] All sessions reset")


EventManager = WakeupSessionManager()
