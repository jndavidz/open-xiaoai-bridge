import asyncio
import os
import threading
import time

from core.ref import get_app, get_speaker, get_xiaoai, get_xiaozhi, set_kws
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
        # VAD 语义（music-far spec 票 02）：
        #   endpoint = 端点器——VAD 只判语音帧与端点时机，主路 KWS 对非暂停态
        #              的每一帧运行（生产配置注释声称的行为，本模式使其成真）；
        #   gate     = 前置门（旧行为，回滚用）——仅 speech_prob ≥ threshold
        #              的帧（加 hangover）进 KWS。远场音乐下会把召回压到 10%。
        self.vad_mode = "endpoint"

        # VAD 状态变量
        self.vad_active = False
        self.vad_speech_frames = 0  # 语音帧计数
        self.vad_silence_frames = 0  # 静音帧计数
        self.vad_min_silence_frames = 15  # 静默超过该帧数则判定为说完
        self.vad_start_time = time.time()

        # 可观测计数（票 02：全帧喂入「可观察、可计数」；kws_counters() 读取）
        self._kws_frames_fed = 0
        self._kws_nonspeech_fed = 0  # VAD 判非语音但仍进 KWS 的帧（endpoint 语义证据）
        self._vad_resets = 0  # 480ms 静音端点 reset 次数

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

        # 影子 KWS（候选模型并行检测，只记录不唤醒；默认关）
        self.shadow = None
        self.shadow_ch0 = None
        self.shadow_l3 = None  # L3 第三臂（票 06），_init_shadow 按配置构建
        self.shadow_audio = None
        self.shadow_stream = None
        self._shadow_always_on = False
        # ch0 臂丢帧观测计数（影子取证：短读率随时间变化是归因关键）
        self._ch0_reads = 0
        self._ch0_short = 0

        self.apply_runtime_config()
        self.config_manager.add_reload_listener(self._on_config_reload)

    def apply_runtime_config(self):
        """同步最新 KWS 相关配置。"""
        vad_config = self.config_manager.get_app_config("vad", {})
        self.vad_threshold = vad_config.get("threshold", 0.10)
        kws_config = self.config_manager.get_app_config("kws", {})
        mode = kws_config.get("vad_mode", "endpoint")
        # 非法值回落端点语义（默认），启动期/热重载均可纠正
        self.vad_mode = mode if mode in ("endpoint", "gate") else "endpoint"
        min_silence_ms = kws_config.get("min_silence_duration", 480)
        self.vad_min_silence_frames = int(min_silence_ms / self.frame_duration_ms)

    def kws_counters(self) -> dict:
        """主路喂帧可观测计数（票 02 验收：全帧喂入可观察、可计数）。"""
        return {
            "vad_mode": self.vad_mode,
            "frames_fed": self._kws_frames_fed,
            "nonspeech_fed": self._kws_nonspeech_fed,
            "vad_resets": self._vad_resets,
        }

    def _on_config_reload(self, *_args):
        """配置重载后刷新运行时参数。"""
        self.apply_runtime_config()

    def _resolve_input_stream(self):
        """按配置选择主路输入流（票 05）。

        audio_input.wakeup_channel 任一态非 inherit ⇒ 读 GlobalStreamKws
        （由 XiaoAiService 的 WakeupInputChain 喂，播放态条件化降声道/AEC）；
        否则维持读 GlobalStream 主链（与历史行为一致）。判定函数与链构建
        共用同一实现，防两侧漂移。
        """
        try:
            from core.services.audio import wakeup_input
            from core.services.audio.stream import GlobalStreamKws

            section = self.config_manager.get_app_config("audio_input", {}) or {}
            channels = section.get("channels", 6)
            if wakeup_input.is_chain_active(section, channels):
                logger.info(
                    "[KWS] 唤醒输入走独立链（GlobalStreamKws）", module="KWS"
                )
                return GlobalStreamKws
        except Exception as exc:  # noqa: BLE001 - 判定失败回落主链
            logger.warning(f"[KWS] wakeup stream resolve failed: {exc}", module="KWS")
        return None

    def start(self):
        self.audio = MyAudio.create()
        source = self._resolve_input_stream()
        self.stream = self.audio.open(
            format=AudioConfig.FORMAT,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=AudioConfig.FRAME_SIZE,
            start=True,
            source=source,
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
        logger.kws_event("关键词唤醒服务启动", f"关键词:[score:{keywords_score}, threshold:{keywords_threshold}], VAD:[mode:{self.vad_mode}, threshold:{self.vad_threshold}, min_silence:{min_silence_ms}ms]")

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

    def _init_shadow(self):
        """按配置初始化影子 KWS；失败只告警，不影响主链路。"""
        self.shadow = None
        self.shadow_ch0 = None
        self.shadow_l3 = None
        self.shadow_audio = None
        self.shadow_stream = None
        self._shadow_always_on = False
        self._ch0_reads = 0
        self._ch0_short = 0
        try:
            config = ConfigManager.instance()
            shadow_config = config.get_app_config("kws.shadow", {}) or {}
            if not shadow_config.get("enabled"):
                return
            model_dir = shadow_config.get("model_dir", "")
            words = config.get_app_config("wakeup.keywords", []) or []
            if not model_dir or not words:
                logger.warning(
                    "[KWS-Shadow] enabled 但缺少 model_dir/keywords，跳过",
                    module="KWS",
                )
                return
            from core.services.audio.kws.shadow import ShadowKws

            arms = shadow_config.get("arms") or ["strongest", "ch0"]
            if isinstance(arms, str):
                arms = [arms]
            arms = [arm for arm in arms if arm in ("strongest", "ch0", "l3")]
            if not arms:
                logger.warning("[KWS-Shadow] arms 为空，跳过", module="KWS")
                return
            max_paths = int(shadow_config.get("max_active_paths", 16))
            # 逐词门槛（票 03）：影子臂与主路同口径（同一词表后缀）
            from core.services.audio.kws.keywords import get_word_gates

            common = dict(
                model_dir=model_dir,
                words=words,
                score=float(shadow_config.get("score", 1.0)),
                threshold=float(shadow_config.get("threshold", 0.25)),
                log_dir=os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "../../../../data/shadow",
                ),
                snapshot_dir=os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "../../../../data/kws_snapshots",
                ),
                max_active_paths=max_paths,
                word_gates=get_word_gates(),
            )
            if "strongest" in arms:
                self.shadow = ShadowKws(arm="strongest", **common)
            if "ch0" in arms:
                from core.services.audio.stream import GlobalStreamCh0

                self.shadow_ch0 = ShadowKws(arm="ch0", **common)
                self.shadow_audio = MyAudio.create()
                self.shadow_stream = self.shadow_audio.open(
                    format=AudioConfig.FORMAT,
                    channels=1,
                    rate=16000,
                    input=True,
                    frames_per_buffer=AudioConfig.FRAME_SIZE,
                    start=True,
                    source=GlobalStreamCh0,
                )
            # L3 第三臂（music-far spec 票 06）：逆向三词模型服务端复现，
            # 与 strongest 臂吃同一份音频，只记录不触发。配置
            # kws.shadow.l3 = {"model_dir": ..., "thresholds": {词: 分}}。
            if "l3" in arms:
                from core.services.audio.kws.l3_shadow import L3ShadowArm

                l3_config = shadow_config.get("l3") or {}
                l3_dir = l3_config.get("model_dir", "")
                if l3_dir:
                    self.shadow_l3 = L3ShadowArm(
                        model_dir=l3_dir,
                        thresholds=l3_config.get("thresholds") or {},
                        log_dir=os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "../../../../data/shadow",
                        ),
                    )
                else:
                    logger.warning(
                        "[KWS-Shadow] arms 含 l3 但 kws.shadow.l3.model_dir 缺失，跳过",
                        module="KWS",
                    )
            self._shadow_always_on = bool(shadow_config.get("always_on", True))
        except Exception as exc:  # noqa: BLE001 - 影子失败不得影响主 KWS
            logger.warning(f"[KWS-Shadow] 初始化异常：{exc}", module="KWS")
            self.shadow = None
            self.shadow_ch0 = None
            self.shadow_l3 = None
            self.shadow_stream = None

    def _shadow_feed(
        self,
        frames: bytes,
        context: dict | None = None,
        arm: str = "strongest",
        main_hit: bool = False,
    ) -> list[str]:
        shadow = {
            "strongest": self.shadow,
            "ch0": self.shadow_ch0,
            "l3": self.shadow_l3,
        }.get(arm)
        if shadow is not None and shadow.enabled and frames:
            return shadow.feed(frames, main_hit=main_hit, context=context)
        return []

    def _shadow_feed_all(
        self,
        frames: bytes,
        ch0_frames: bytes,
        context: dict | None = None,
        main_hit: bool = False,
    ) -> None:
        """三臂同帧喂入（strongest 与 L3 吃同一份音频；ch0 吃独立流帧）。"""
        self._shadow_feed(frames, context=context, arm="strongest", main_hit=main_hit)
        self._shadow_feed(frames, context=context, arm="l3", main_hit=main_hit)
        if ch0_frames:
            self._shadow_feed(ch0_frames, context=context, arm="ch0", main_hit=main_hit)

    def _shadow_reset(self) -> None:
        for shadow in (self.shadow, self.shadow_ch0, self.shadow_l3):
            if shadow is not None:
                shadow.reset()

    def _resolve_device_state(self, xiaozhi) -> str | None:
        """设备状态权威源 = MainApp.device_state。

        生产部署 XIAOZHI_ENABLE 未设置时 get_xiaozhi() 为 None，旧实现直接读
        xiaozhi 导致影子日志 device_state 恒 null（观测盲区）；MainApp 单例
        始终存在，xiaozhi.device_state 本身就是它的代理，故优先取 app。
        """
        app = get_app()
        state = getattr(app, "device_state", None) if app is not None else None
        if state is None and xiaozhi is not None:
            state = getattr(xiaozhi, "device_state", None)
        if hasattr(state, "name"):
            state = state.name
        if state is not None and not isinstance(state, str):
            state = str(state)
        return state

    def _shadow_context(self, xiaozhi, speech_prob, paused: bool) -> dict:
        gate_count, gate_reasons = self.gate_status()
        state = self._resolve_device_state(xiaozhi)
        speaker = get_speaker()
        playing = getattr(speaker, "status", None) if speaker is not None else None
        return {
            "vad_prob": None if speech_prob is None else round(float(speech_prob), 4),
            "device_state": str(state) if state is not None else None,
            # 误触发归因关键字段：设备侧媒体播放状态（playing/paused/idle）
            "playing": playing if isinstance(playing, str) else None,
            "paused": bool(paused),
            "listen_disabled": bool(self.listen_disabled),
            "gate_count": int(gate_count),
            "gate_reasons": list(gate_reasons),
            # ch0 臂读帧累计：ch0_short/ch0_reads 即丢帧率的分子/分母
            "ch0_reads": int(getattr(self, "_ch0_reads", 0)),
            "ch0_short": int(getattr(self, "_ch0_short", 0)),
        }

    def _handle_main_hit(self, result: dict, speech_prob: float, context: dict) -> None:
        self._log_wakeup_evidence(result, speech_prob)
        logger.wakeup(result["keyword"], module="KWS")
        self.on_message(result["keyword"])
        if self._shadow_always_on:
            for shadow in (self.shadow, self.shadow_ch0, self.shadow_l3):
                if shadow is not None and shadow.enabled:
                    shadow.record_main_hit(result["keyword"], context=context)

    # ------------------------------------------------------------------
    # 主路单帧步进（票 02：从 _detection_loop 提出为可测方法——测试对
    # SherpaOnnx/Silero 打桩后直接调用，不起线程）。
    # 语义按 vad_mode 分派：
    #   endpoint —— 非暂停态每一帧都进 KWS；VAD 只负责判端点（静音 480ms
    #                reset 检测流，会话切分与回声丢弃依赖它，不得删除）。
    #   gate     —— 旧行为：仅语音帧 + hangover（≤min_silence 的静音帧）进 KWS。
    # 返回命中关键词（无命中 None）。
    # ------------------------------------------------------------------
    def _main_step(self, frames, speech_prob, context, ch0_frames=b""):
        if self.vad_mode == "gate":
            return self._main_step_gate(frames, speech_prob, context, ch0_frames)
        return self._main_step_endpoint(frames, speech_prob, context, ch0_frames)

    def _main_step_endpoint(self, frames, speech_prob, context, ch0_frames):
        """端点语义：全帧喂 KWS，VAD 只管端点与 reset。"""
        is_speech = speech_prob is not None and speech_prob >= self.vad_threshold
        if is_speech:
            if not self.vad_active:
                self.vad_active = True
                self.vad_start_time = time.time()
                logger.debug("检测到语音，开始 KWS 检测", module="KWS")
            self.vad_silence_frames = 0
        else:
            # 静音帧一律计数（含从未激活的长静音）：每满 480ms reset 一次检测流
            self.vad_silence_frames += 1

        # 端点：静音超过 min_silence ⇒ reset 检测流（会话切分与回声丢弃），
        # reset 后当前静音帧重新开始累积。
        if self.vad_silence_frames > self.vad_min_silence_frames:
            duration_ms = self.vad_silence_frames * self.frame_duration_ms
            logger.debug(
                f"检测到持续静音（{duration_ms:.0f}ms），重置检测流",
                module="KWS",
            )
            self.vad_active = False
            self.vad_silence_frames = 0
            SherpaOnnx.reset()
            self._shadow_reset()
            self._vad_resets += 1

        # 全帧喂 KWS（含 VAD 判非语音的帧——票 02 的核心语义）
        self._kws_frames_fed += 1
        if not is_speech:
            self._kws_nonspeech_fed += 1
        result = SherpaOnnx.kws(frames)
        if not self._shadow_always_on:
            self._shadow_feed_all(
                frames, ch0_frames, context=context, main_hit=bool(result)
            )
        if result:
            self._handle_main_hit(result, speech_prob, context)
            self.vad_active = False
            self.vad_silence_frames = 0
        return result

    def _main_step_gate(self, frames, speech_prob, context, ch0_frames):
        """前置门语义（旧实现原样保留，回滚用）。"""
        is_speech = speech_prob is not None and speech_prob >= self.vad_threshold
        result = None
        if is_speech:
            if not self.vad_active:
                self.vad_active = True
                self.vad_start_time = time.time()
                logger.debug("检测到语音，开始 KWS 检测", module="KWS")

            self.vad_silence_frames = 0

            result = SherpaOnnx.kws(frames)
            if not self._shadow_always_on:
                # 三臂同帧（含 L3——回滚组合不得静默断供，spec 票 06）
                self._shadow_feed_all(
                    frames, ch0_frames, context=context, main_hit=bool(result)
                )
            if result:
                self._handle_main_hit(result, speech_prob, context)
                self.vad_active = False
                self.vad_silence_frames = 0
        else:
            # 静音处理
            if self.vad_active:
                self.vad_silence_frames += 1

                # 在激活状态下，允许一定的静音间隙
                if self.vad_silence_frames <= self.vad_min_silence_frames:
                    result = SherpaOnnx.kws(frames)
                    if not self._shadow_always_on:
                        self._shadow_feed_all(
                            frames, ch0_frames, context=context, main_hit=bool(result)
                        )
                    if result:
                        self._handle_main_hit(result, speech_prob, context)
                        self.vad_active = False
                        self.vad_silence_frames = 0
                else:
                    # 静音超过阈值，停止处理
                    duration_ms = self.vad_silence_frames * self.frame_duration_ms
                    active_duration_ms = (
                        (time.time() - self.vad_start_time) * 1000
                        if hasattr(self, "vad_start_time")
                        else -1
                    )
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
                    self._shadow_reset()
                    self._vad_resets += 1
        return result

    def _process_frame(self, frames: bytes, ch0_frames: bytes = b""):
        """处理一帧（含暂停判定）：暂停态完全不动主路与影子；返回命中或 None。

        从 _detection_loop 提出（票 02）：暂停/非暂停语义在测试中可直接驱动，
        无需起线程。
        """
        xiaozhi = get_xiaozhi()
        device_state = self._resolve_device_state(xiaozhi)
        paused = bool(
            self.paused
            or self.listen_disabled
            or device_state in [DeviceState.LISTENING, DeviceState.SPEAKING]
        )
        speech_prob = (
            None
            if paused
            else (Silero.vad(frames, self.sample_rate) or 0)
        )
        context = self._shadow_context(xiaozhi, speech_prob, paused)

        # 影子 always-on：主 KWS 之前的全帧检测（含 VAD 未放行帧）；
        # L3 第三臂与 strongest 同一份音频（票 06）
        if self._shadow_always_on:
            self._shadow_feed_all(frames, ch0_frames, context=context)

        if paused:
            return None

        # 命中留证环形缓冲（incident §5 建议 6）：保留最近 N 秒音频
        self._audio_ring.append(frames)
        if len(self._audio_ring) > self._ring_max_frames:
            del self._audio_ring[0 : len(self._audio_ring) - self._ring_max_frames]

        # 主路单帧步进（语义按 vad_mode 分派，见 _main_step 注释）
        return self._main_step(frames, speech_prob, context, ch0_frames)

    def _detection_loop(self):
        SherpaOnnx.start()
        self._init_shadow()
        self.stream.start_stream()
        while True:
            # 主路（strongest）与影子 ch0 各自从独立 GlobalStream 读取
            frames = self.stream.read(self.frame_size)
            if self.shadow_stream is not None:
                ch0_frames = self.shadow_stream.read(self.frame_size)
                self._ch0_reads += 1
                if len(ch0_frames) != self.frame_size * 2:
                    ch0_frames = b""
                    self._ch0_short += 1
            else:
                ch0_frames = b""
            if len(frames) != self.frame_size * 2:
                time.sleep(0.01)
                continue

            self._process_frame(frames, ch0_frames)

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
