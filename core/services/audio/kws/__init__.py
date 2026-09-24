import asyncio
import os
import threading
import time

from core.ref import get_app, get_xiaoai, get_xiaozhi, set_kws
from core.services.audio.kws.sherpa import SherpaOnnx
from core.services.audio.stream import MyAudio
from core.services.audio.vad.silero import Silero
from core.services.protocols.typing import AudioConfig, DeviceState
from core.utils.config import ConfigManager
from core.utils.logger import logger
from core.wakeup_session import EventManager


def _fmt_ts(timestamps) -> str:
    """时间戳列表格式化为 '起始-结束' 字符串（留证用）。"""
    try:
        ts = list(timestamps or [])
    except Exception:
        return ""
    if not ts:
        return ""
    if len(ts) == 1:
        return f"{ts[0]:.2f}"
    return f"{ts[0]:.2f}-{ts[-1]:.2f}"


class _KWS:
    def __init__(self):
        set_kws(self)
        self.config_manager = ConfigManager.instance()
        self.vad_threshold = 0.10
        
        # VAD 状态变量
        self.vad_active = False
        self.vad_speech_frames = 0  # 语音帧计数
        self.vad_silence_frames = 0  # 静音帧计数
        self.vad_min_silence_frames = 15  # 静默超过该帧数则判定为说完
        self.vad_start_time = time.time()
        
        # 设置帧大小为 512 以兼容 Silero VAD
        self.frame_size = 512
        self.sample_rate = 16000
        self.frame_duration_ms = (self.frame_size * 1000) / self.sample_rate  # 32ms per frame

        # 播放闸门状态（incident §5 建议 1，详见 gate_on/gate_off）
        self._gate_lock = threading.Lock()
        self._gate_count = 0
        self._gate_reasons: list[str] = []
        self._gate_timer: threading.Timer | None = None

        # 命中留证：语音环形缓冲（incident §5 建议 6，详见 _log_wakeup_evidence）
        # 保存最近 _AUDIO_SNAPSHOT_SECONDS 秒的 16k/单声道/16bit PCM
        self._AUDIO_SNAPSHOT_SECONDS = 5
        self._MAX_SNAPSHOTS = 10
        self._SNAPSHOT_DIR = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "../../../../data/kws_snapshots"
        )
        # 每帧 512 样本 * 2 字节；保留 5s => 5000/32 = 157 帧
        self._ring_max_frames = int(
            self._AUDIO_SNAPSHOT_SECONDS * 1000 / ((self.frame_size * 1000) / self.sample_rate)
        )
        self._audio_ring: list[bytes] = []

        self.apply_runtime_config()
        self.config_manager.add_reload_listener(self._on_config_reload)

    def apply_runtime_config(self):
        """同步最新 KWS 相关配置。"""
        vad_config = self.config_manager.get_app_config("vad", {})
        self.vad_threshold = vad_config.get("threshold", 0.10)
        kws_config = self.config_manager.get_app_config("kws", {})
        min_silence_ms = kws_config.get("min_silence_duration", 480)
        self.vad_min_silence_frames = int(min_silence_ms / self.frame_duration_ms)

    def _on_config_reload(self, *_args):
        """配置重载后刷新运行时参数。"""
        self.apply_runtime_config()

    def start(self):
        self.audio = MyAudio.create()
        self.stream = self.audio.open(
            format=AudioConfig.FORMAT,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=AudioConfig.FRAME_SIZE,
            start=True,
        )

        # 启动 KWS 服务
        self._conv_paused = False   # 会话流程暂停（由 pause()/resume() 控制）
        self.listen_disabled = False  # T7.6「停止聆听」持久开关（高于 paused，resume 不可撤销）
        # 注：self.paused 是派生属性（_conv_paused or 闸门计数>0），不可直接赋值
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()
        config = ConfigManager.instance()
        keywords_score = config.get_app_config("kws.keywords_score", 2.0)
        keywords_threshold = config.get_app_config("kws.keywords_threshold", 0.2)
        min_silence_ms = config.get_app_config("kws.min_silence_duration", 480)
        logger.kws_event("关键词唤醒服务启动", f"关键词:[score:{keywords_score}, threshold:{keywords_threshold}], VAD:[threshold:{self.vad_threshold}, min_silence:{min_silence_ms}ms]")

    def get_file_path(self, file_name: str):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "../../../models", file_name)

    def pause(self):
        """会话流程暂停（wakeup_session 进入 AI 对话时调用）。"""
        self._conv_paused = True

    def resume(self):
        # T7.6：用户已显式"停止聆听"时，resume 不生效，保持暂停
        if self.listen_disabled:
            return
        self._conv_paused = False
        # AGENTS.md:151-158 教训：恢复时必须 reset Sherpa 流，
        # 否则暂停期间的音频（含刚刚的 TTS 播报回声）会泄漏到下一轮检测。
        # 仅在闸门也已归零（真正恢复监听）时 reset。
        if self._gate_count == 0:
            self._safe_reset()

    @property
    def paused(self) -> bool:
        """派生状态：会话暂停 OR 播放闸门计数 > 0。

        incident-kws-self-trigger-loop.md §5: 播报期间必须停 KWS（闸门），
        而会话流程（pause/resume）也在停 KWS。两者若各写一个 `paused` 标志会互相踩踏，
        故统一为派生属性：任一来源要求暂停则暂停。"""
        return self._conv_paused or self._gate_count > 0

    # ------------------------------------------------------------------
    # 播放闸门（incident-kws-self-trigger-loop.md §5 建议 1）
    # 目的：TTS 播报期间暂停 KWS，避免播报文案命中自身词表形成自触发回环。
    # 与 pause()/resume() 的区别：
    #   - 引用计数（多个播报并发时不互相踩踏；与会话暂停正交）
    #   - 自动恢复（即使调用方忘记 gate_off / 抛异常，timer 也会兜底）
    #   - 恢复时 reset Sherpa 流（丢弃播报回声帧，防泄漏到下一轮，见 AGENTS.md:151-158）
    # ------------------------------------------------------------------
    def gate_on(self, reason: str = "", max_seconds: float = 120.0, token: str | None = None) -> str:
        """开启播放闸门。返回 token。引用计数 +1，并挂一个兜底自动恢复 timer。

        max_seconds: 兜底时限（防调用方异常导致 KWS 永久失效）。
        """
        with self._gate_lock:
            self._gate_count += 1
            self._gate_reasons.append(reason)
            if self._gate_timer is not None:
                self._gate_timer.cancel()
                self._gate_timer = None
            if token is None:
                token = f"gate-{int(time.monotonic()*1000)}-{self._gate_count}"
            self._gate_timer = threading.Timer(max_seconds, self._gate_autoresume, args=(token,))
            self._gate_timer.daemon = True
            self._gate_timer.start()
            count = self._gate_count
        logger.debug(
            f"[KWS] 播放闸门 +1 (count={count}, reason={reason!r}, 兜底={max_seconds:.0f}s)",
            module="KWS",
        )
        return token

    def gate_off(self, token: str | None = None):
        """关闭播放闸门。引用计数 -1；归零时 reset Sherpa 流。"""
        with self._gate_lock:
            if self._gate_count > 0:
                self._gate_count -= 1
                if self._gate_reasons:
                    self._gate_reasons.pop()
            left = self._gate_count
            if left > 0:
                return
            if self._gate_timer is not None:
                self._gate_timer.cancel()
                self._gate_timer = None
        if not self._conv_paused:
            self._safe_reset()
            logger.debug("[KWS] 播放闸门归零，KWS 恢复 + Sherpa 流已 reset", module="KWS")
        else:
            logger.debug(
                "[KWS] 播放闸门归零，但会话仍处于暂停态（等 resume）",
                module="KWS",
            )

    def _safe_reset(self):
        """重置 Sherpa 流（丢弃播报回声帧）。失败不抛。"""
        try:
            SherpaOnnx.reset()
        except Exception:
            pass

    def _gate_autoresume(self, token: str):
        """兜底：超过 max_seconds 未解除闸门时强制清零，避免 KWS 永久失效。"""
        with self._gate_lock:
            if self._gate_count <= 0:
                return
            logger.warning(
                f"[KWS] 播放闸门超时兜底恢复 (count={self._gate_count}, token={token}) "
                f"——调用方可能未正常 gate_off",
                module="KWS",
            )
            self._gate_count = 0
            self._gate_reasons.clear()
            self._gate_timer = None
        self._safe_reset()

    def is_gated(self) -> bool:
        return self._gate_count > 0

    def gate_status(self) -> tuple[int, list[str]]:
        """返回 (计数, 原因列表)，供可观测性使用。"""
        return self._gate_count, list(self._gate_reasons)

    def disable_listening(self):
        """T7.6「停止聆听」：持久停止 KWS 关键词分析（resume 不可撤销）。"""
        self.listen_disabled = True
        self._conv_paused = True

    def enable_listening(self):
        """T7.6 恢复：经 HTTP AUDIO_INPUT 通道解除停止聆听。"""
        self.listen_disabled = False
        self._conv_paused = False

    def is_listening_disabled(self) -> bool:
        return self.listen_disabled

    def _detection_loop(self):
        SherpaOnnx.start()
        self.stream.start_stream()
        while True:
            # 读取缓冲区音频数据
            frames = self.stream.read(self.frame_size)
            if len(frames) != self.frame_size * 2:
                time.sleep(0.01)
                continue

            # 在说话和监听状态时，暂停 KWS
            xiaozhi = get_xiaozhi()
            if (
                not frames
                or self.paused
                or self.listen_disabled
                or (
                    xiaozhi and xiaozhi.device_state
                    in [DeviceState.LISTENING, DeviceState.SPEAKING]
                )
            ):
                time.sleep(0.01)
                continue

            # 命中留证环形缓冲（incident §5 建议 6）：保留最近 N 秒音频
            self._audio_ring.append(frames)
            if len(self._audio_ring) > self._ring_max_frames:
                del self._audio_ring[0 : len(self._audio_ring) - self._ring_max_frames]

            # 先进行 VAD 检测
            speech_prob = Silero.vad(frames, self.sample_rate) or 0
            is_speech = speech_prob >= self.vad_threshold
            
            if is_speech:
                # 检测到语音，立即激活
                if not self.vad_active:
                    self.vad_active = True
                    self.vad_start_time = time.time()
                    logger.debug("检测到语音，开始 KWS 检测", module="KWS")
                
                self.vad_silence_frames = 0
                
                # 只在有语音时才进行 KWS 检测
                result = SherpaOnnx.kws(frames)
                if result:
                    self._log_wakeup_evidence(result, speech_prob)
                    logger.wakeup(result["keyword"], module="KWS")
                    self.on_message(result["keyword"])
                    # 唤醒后重置状态
                    self.vad_active = False
                    self.vad_silence_frames = 0
                    
            else:
                # 静音处理
                if self.vad_active:
                    self.vad_silence_frames += 1
                    
                    # 在激活状态下，允许一定的静音间隙
                    if self.vad_silence_frames <= self.vad_min_silence_frames:
                        # 继续将音频送入 KWS，允许短暂的静音
                        result = SherpaOnnx.kws(frames)
                        if result:
                            self._log_wakeup_evidence(result, speech_prob)
                            logger.wakeup(result["keyword"], module="KWS")
                            self.on_message(result["keyword"])
                            self.vad_active = False
                            self.vad_silence_frames = 0
                    else:
                        # 静音超过阈值，停止处理
                        duration_ms = self.vad_silence_frames * self.frame_duration_ms
                        active_duration_ms = (time.time() - self.vad_start_time) * 1000 if hasattr(self, 'vad_start_time') else -1
                        logger.debug(
                            (
                                f"检测到持续静音（{duration_ms:.0f}ms），暂停 KWS，"
                                f"本次 KWS 监听时长 {active_duration_ms:.0f}ms"
                            ),
                            module="KWS",
                        )
                        self.vad_active = False
                        self.vad_silence_frames = 0
                        # Reset Sherpa stream to discard partial recognition state,
                        # preventing leftover audio from the previous utterance
                        # from combining with the next one.
                        SherpaOnnx.reset()

    def on_message(self, text: str):
        loop = get_app().loop if get_app() else get_xiaoai().async_loop
        logger.debug(f"[KWS] Dispatch wakeup event: {text}")
        future = asyncio.run_coroutine_threadsafe(
            EventManager.wakeup(text, "kws"),
            loop,
        )

        def _log_result(done_future):
            try:
                done_future.result()
            except Exception as exc:
                logger.error(f"[KWS] Wakeup dispatch failed: {type(exc).__name__}: {exc}")

        future.add_done_callback(_log_result)

    # ------------------------------------------------------------------
    # 命中留证（incident-kws-self-trigger-loop.md §5 建议 6）
    # 事故复盘靠“猜声源”；这里记录命中时的可观测上下文（含音频快照）。
    # 注：sherpa-onnx 的 KeywordResult 不含置信度，故记 tokens/timestamps 代替。
    # ------------------------------------------------------------------
    def _log_wakeup_evidence(self, result: dict, speech_prob: float):
        """记录 KWS 命中证据：关键词/tokens/时间戳/VAD prob + 前 N 秒音频快照。"""
        try:
            snap_path = self._dump_audio_snapshot()
            logger.info(
                "[KWS] 命中留证 | "
                f"keyword={result.get('keyword')!r} "
                f"vad_prob={speech_prob:.3f} "
                f"tokens={len(result.get('tokens') or [])} "
                f"ts=[{_fmt_ts(result.get('timestamps'))}] "
                f"snapshot={snap_path or '(不可用)'}",
                module="KWS",
            )
        except Exception as exc:
            # 留证失败绝不能影响唤醒主流程
            logger.debug(f"[KWS] 命中留证失败: {type(exc).__name__}: {exc}", module="KWS")
        # 快照后清空环形缓冲（避免下一轮重复）
        try:
            self._audio_ring.clear()
        except Exception:
            pass

    def _dump_audio_snapshot(self):
        """将环形缓冲的最近音频写入文件，返回路径（或 None）。

        采样率 16k/单声道/16bit；时长由 _AUDIO_SNAPSHOT_SECONDS 决定。
        失败返回 None（不影响主流程）。
        """
        if not self._audio_ring:
            return None
        import os as _os
        import time as _time

        os.makedirs(self._SNAPSHOT_DIR, exist_ok=True)
        path = _os.path.join(
            self._SNAPSHOT_DIR,
            f"kws_hit_{_time.strftime('%Y%m%d-%H%M%S')}_{int(_time.time()*1000) % 1000:03d}.pcm",
        )
        with open(path, "wb") as f:
            f.write(b"".join(self._audio_ring))
        # 只保留最近 N 个快照，防磁盘增长
        try:
            files = sorted(
                p for p in _os.listdir(self._SNAPSHOT_DIR) if p.startswith("kws_hit_")
            )
            for old in files[: -self._MAX_SNAPSHOTS]:
                _os.unlink(_os.path.join(self._SNAPSHOT_DIR, old))
        except Exception:
            pass
        return path


KWS = _KWS()
