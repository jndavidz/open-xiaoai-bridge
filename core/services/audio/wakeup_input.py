"""按播放态选择唤醒输入通道（music-far spec 票 05）。

唤醒检测的输入通道不再是一个全局单值：播放期可走回声消除输出或固定单麦，
空闲期走逐段最强麦。关键边界（spec Implementation Decisions「输入链」）：
**只改 KWS/VAD 消费路径**——对话 ASR 与语音注入的降声道行为（GlobalStream
主链）完全不经本模块，逐字节不变。

链路：
    raw 多声道交错 PCM（XiaoAiService.on_input_data）
      → WakeupInputChain.feed（按播放态选 strongest / fixed / aec）
      → GlobalStreamKws（独立 GlobalStream，仿 GlobalStreamCh0 先例）
      → 主路 KWS 读流

模式取值（audio_input.wakeup_channel.playing / .idle）：
    "inherit"  = 不启用独立链，KWS 继续读 GlobalStream（默认，零行为变化）
    "strongest"= 滑动窗口最强麦 + 迟滞（同主链口径）
    "fixed"    = 固定声道（fixed_channel，默认 ch0）
    "aec"      = 回声消除输出（播放期参考 ch6；需要 8ch 采集含参考声道）
    "specsub"  = 谱减回声抑制输出（同一参考声道；实测优于 aec，见 specsub.py 头注释）
    "sidecar"  = 独立 8ch 采集器（sidecar.py，票 01）→ 谱减 → 1ch。近端与参考
                 同源（同一 arecord 流的 ch0–5 与 ch6）；sidecar 未就绪/断流/
                 帧非法 ⇒ 自动回落 strongest（client 6ch 选麦），计数告警。
非法值启动期告警并回落 "inherit"，不静默。

播放态判定使用既有宿主状态（speaker.status == "playing"），不在音频路径里
新造状态源。AEC 本体见 aec.py（默认关闭）。
"""

from __future__ import annotations

import time

from core.services.audio.channels import (
    STRATEGY_FIXED,
    STRATEGY_STRONGEST,
    ChannelDownmixer,
)
from core.services.audio.sidecar import SIDECAR_CHANNELS
from core.utils.logger import logger

MODE_INHERIT = "inherit"
MODE_STRONGEST = STRATEGY_STRONGEST
MODE_FIXED = STRATEGY_FIXED
MODE_AEC = "aec"
MODE_SPECSUB = "specsub"
MODE_SIDECAR = "sidecar"
VALID_MODES = (MODE_INHERIT, MODE_STRONGEST, MODE_FIXED, MODE_AEC, MODE_SPECSUB,
               MODE_SIDECAR)

DEFAULT_WAKEUP_CHANNEL = {"playing": MODE_INHERIT, "idle": MODE_INHERIT, "fixed_channel": 0}
DEFAULT_AEC = {
    "enabled": False,
    "ref_channel": 6,
    "mic_channel": 0,
    "step_size": 0.8,
    "ref_lead_samples": 64,
    "ref_polarity_inverted": True,
}


def _norm_mode(value, label: str, fallback: str = MODE_INHERIT) -> str:
    """归一化模式值；非法值告警并回落（启动期可见，不静默）。"""
    if isinstance(value, str) and value.strip().lower() in VALID_MODES:
        return value.strip().lower()
    if value is None:
        return fallback
    logger.warning(
        f"[WakeupInput] {label} 非法值 {value!r}（合法：{'/'.join(VALID_MODES)}），"
        f"回落 {fallback!r}",
        module="XiaoAI",
    )
    return fallback


def parse_wakeup_channel_config(section: dict | None) -> dict:
    """解析 audio_input.wakeup_channel；非法值回落 inherit（带告警）。"""
    section = section if isinstance(section, dict) else {}
    try:
        fixed_channel = int(section.get("fixed_channel", 0))
    except (TypeError, ValueError):
        fixed_channel = 0
    return {
        "playing": _norm_mode(section.get("playing"), "wakeup_channel.playing"),
        "idle": _norm_mode(section.get("idle"), "wakeup_channel.idle"),
        "fixed_channel": max(0, fixed_channel),
    }


def is_chain_active(section: dict | None, channels: int) -> bool:
    """配置是否激活独立唤醒输入链（双方共用同一判定，防两侧漂移）。"""
    parsed = parse_wakeup_channel_config(section)
    if channels < 2:
        return False  # 单声道采集无可选通道，恒 inherit
    return parsed["playing"] != MODE_INHERIT or parsed["idle"] != MODE_INHERIT


def resolve_aec_params(section: dict | None, channels: int) -> dict | None:
    """解析 audio_input.aec；不可用（未启用/无参考声道）返回 None。"""
    section = section if isinstance(section, dict) else {}
    if not section.get("enabled"):
        return None
    params = dict(DEFAULT_AEC)
    params.update({k: v for k, v in section.items() if k != "enabled"})
    ref_channel = int(params.get("ref_channel", 6))
    if ref_channel >= channels:
        logger.warning(
            f"[WakeupInput] aec.ref_channel={ref_channel} 超出采集声道数 "
            f"{channels}（需 8ch 采集含播放参考），AEC 不可用",
            module="XiaoAI",
        )
        return None
    return params


def resolve_specsub_params(section: dict | None, channels: int) -> dict | None:
    """解析 audio_input.specsub；不可用（未启用/无参考声道）返回 None。"""
    section = section if isinstance(section, dict) else {}
    if not section.get("enabled"):
        return None
    params = dict(section)
    ref_channel = int(params.get("ref_channel", 6))
    if ref_channel >= channels:
        logger.warning(
            f"[WakeupInput] specsub.ref_channel={ref_channel} 超出采集声道数 "
            f"{channels}（需 8ch 采集含播放参考），谱减不可用",
            module="XiaoAI",
        )
        return None
    return params


def _build_specsub(channels: int, ref_channel: int, mic_channel: int,
                   params: dict) -> "SpectralSuppressor":
    """按统一调优段构造谱减器（client 流与 sidecar 流共用同一参数口径）。"""
    from core.services.audio.specsub import SpectralSuppressor

    return SpectralSuppressor(
        channels=channels,
        ref_channel=ref_channel,
        mic_channel=mic_channel,
        alpha=float(params.get("alpha", 0.6)),
        floor_db=float(params.get("floor_db", -20.0)),
        ref_lead_samples=int(params.get("ref_lead_samples", 64)),
    )


class WakeupInputChain:
    """按播放态选择唤醒输入通道并产出 1ch PCM（喂 GlobalStreamKws）。

    `feed(raw_bytes)` 输入与 XiaoAiService.on_input_data 相同的多声道交错
    int16 PCM；返回 1ch int16。主链（GlobalStream）的降声道不由本类触碰。
    sidecar 模式下 client 流在有效态不投递（`feed` 返回空），8ch 帧经
    `feed_sidecar(frame)` 专用入口进入（近端与参考同源，见 sidecar.py）。
    """

    def __init__(self, config: dict, channels: int) -> None:
        parsed = parse_wakeup_channel_config(config.get("wakeup_channel"))
        self.playing_mode = parsed["playing"]
        self.idle_mode = parsed["idle"]
        self.fixed_channel = parsed["fixed_channel"]
        self.channels = int(channels)
        self._aec_params = resolve_aec_params(config.get("aec"), channels)
        self._specsub_params = resolve_specsub_params(config.get("specsub"), channels)
        self._specsub = None
        self._mixers: dict[str, ChannelDownmixer] = {}
        self._aec = None
        # 参考声道（music-far 缺口 2）：与主链同源配置，8ch 时排除 ch6/7
        raw_refs = config.get("reference_channels") or []
        try:
            self.reference_channels = tuple(
                c for c in (int(c) for c in raw_refs) if 0 <= c < self.channels
            )
        except (TypeError, ValueError):
            self.reference_channels = ()
        if self._uses_aec:
            from core.services.audio.aec import EchoCanceller

            p = self._aec_params
            mic_channel = int(p.get("mic_channel", 0))
            if mic_channel != self.fixed_channel:
                logger.warning(
                    f"[WakeupInput] aec.mic_channel={mic_channel} 与 "
                    f"fixed_channel={self.fixed_channel} 不一致，按 fixed_channel 取麦",
                    module="XiaoAI",
                )
            self._aec = EchoCanceller(
                channels=self.channels,
                ref_channel=int(p.get("ref_channel", 6)),
                mic_channel=self.fixed_channel,
                step_size=float(p.get("step_size", 0.8)),
                ref_lead_samples=int(p.get("ref_lead_samples", 64)),
                ref_polarity_inverted=bool(p.get("ref_polarity_inverted", True)),
            )
        if self._uses_specsub:
            p = self._specsub_params
            self._specsub = _build_specsub(
                self.channels, int(p.get("ref_channel", 6)), self.fixed_channel, p
            )
        # sidecar 模式（lx06-audio-sidecar spec 票 02）：8ch 采集器专属谱减器，
        # 与 client 流谱减（6ch，ref 恒不可用）物理隔离——近端与参考必须同源
        # （spec 硬约束 1：禁止 sidecar ch6 与 client 6ch 拼接）。
        self._sidecar_channels = SIDECAR_CHANNELS
        self._sidecar_specsub = None
        self._sidecar_healthy = False
        self._sidecar_health_at = 0.0
        self._sidecar_stale_window = 5.0
        self._sidecar_fallback_announced = False
        if MODE_SIDECAR in (self.playing_mode, self.idle_mode):
            try:
                sc = config.get("sidecar") if isinstance(config, dict) else None
                sc = sc if isinstance(sc, dict) else {}
                try:
                    self._sidecar_channels = max(
                        2, min(int(sc.get("channels", SIDECAR_CHANNELS)), 8)
                    )
                except (TypeError, ValueError):
                    pass
                try:
                    self._sidecar_stale_window = max(
                        1.0, min(float(sc.get("stall_timeout", 2.0)) * 2.5, 60.0)
                    )
                except (TypeError, ValueError):
                    self._sidecar_stale_window = 5.0
                # 谱减参数沿用 audio_input.specsub 的调优段（模式本身即开关，
                # 不要求 specsub.enabled）；参考声道沿用既有 reference_channels
                # 配置面（spec：直接沿用不新造概念）——LX06 = [6, 7] 取首个，
                # 未声明则回退 specsub.ref_channel。
                p = dict(config.get("specsub") or {}) if isinstance(config, dict) else {}
                raw_refs = (config.get("reference_channels") or []) \
                    if isinstance(config, dict) else []
                try:
                    refs = [int(c) for c in raw_refs]
                except (TypeError, ValueError):
                    refs = []
                ref_channel = refs[0] if refs else int(p.get("ref_channel", 6))
                if not 0 <= ref_channel < self._sidecar_channels:
                    raise ValueError(
                        f"sidecar ref_channel {ref_channel} 越界"
                        f"（channels={self._sidecar_channels}）"
                    )
                self._sidecar_specsub = _build_specsub(
                    self._sidecar_channels, ref_channel, self.fixed_channel, p
                )
            except Exception as exc:  # noqa: BLE001 - 构建失败回落 strongest
                logger.warning(
                    f"[WakeupInput] sidecar 谱减器构建失败（{exc}），回落 strongest",
                    module="XiaoAI",
                )
                self._sidecar_specsub = None
        self._last_mode = None
        self._counters = {"frames_in": 0, "mode_switches": 0, "aec_bypass": 0,
                          "specsub_bypass": 0, "sidecar_frames": 0,
                          "sidecar_invalid_frames": 0, "sidecar_fallbacks": 0}

    @property
    def active(self) -> bool:
        return self.playing_mode != MODE_INHERIT or self.idle_mode != MODE_INHERIT

    @property
    def _uses_specsub(self) -> bool:
        return (MODE_SPECSUB in (self.playing_mode, self.idle_mode)
                and self._specsub_params is not None)

    @property
    def _uses_aec(self) -> bool:
        return MODE_AEC in (self.playing_mode, self.idle_mode) and self._aec_params is not None

    def _mode_for(self, playing: bool) -> str:
        mode = self.playing_mode if playing else self.idle_mode
        # aec 配置了但不可用（无参考声道）→ 静默回落固定单麦（旁路语义）
        if mode == MODE_AEC and self._aec_params is None:
            return MODE_FIXED
        if mode == MODE_SPECSUB and self._specsub_params is None:
            return MODE_FIXED
        # sidecar 未就绪/断流/未配置 ⇒ 回落 strongest 单麦（spec 硬约束 4）
        if mode == MODE_SIDECAR and not self._sidecar_ready():
            return MODE_STRONGEST
        return mode

    def _sidecar_ready(self) -> bool:
        """sidecar 可投递：谱减器在位且健康状态新鲜（由 set_sidecar_health 供）。"""
        if self._sidecar_specsub is None or not self._sidecar_healthy:
            return False
        return (time.monotonic() - self._sidecar_health_at) <= self._sidecar_stale_window

    def set_sidecar_health(self, healthy) -> None:
        """由宿主逐帧传入 sidecar 采集器健康态（None = 无采集器）。

        健康 → 不健康的跳变计数并告警；**首次评估即不健康（未就绪回落）也
        计数告警一次**（spec：回落可见，不静默）。
        """
        prev_ready = self._sidecar_ready()
        self._sidecar_healthy = bool(healthy)
        if self._sidecar_healthy:
            self._sidecar_health_at = time.monotonic()
        if self._sidecar_ready():
            if not prev_ready:
                logger.info(
                    "[WakeupInput] sidecar 就绪，唤醒输入走 sidecar+谱减",
                    module="XiaoAI",
                )
            return
        if prev_ready or not self._sidecar_fallback_announced:
            self._sidecar_fallback_announced = True
            self._counters["sidecar_fallbacks"] += 1
            logger.warning(
                "[WakeupInput] sidecar 未就绪/断流，唤醒输入回落 strongest 单麦",
                module="XiaoAI",
            )

    def feed_sidecar(self, frame: bytes) -> bytes:
        """sidecar 8ch 帧 → 谱减 → 1ch int16（sidecar 模式专用入口）。

        只接受 sidecar 自己的 8ch 流：client 的 6ch 永远进不了谱减（跨流
        拼接被 spec 明确禁止——两流各有缓冲、偏移漂移，谱减对错位极敏感）。
        非 sidecar 有效态（未就绪/已回落/配置不含 sidecar）一律丢弃。
        """
        if not frame or self._sidecar_specsub is None:
            return b""
        playing = getattr(self, "_playing", False)
        if self._mode_for(playing) != MODE_SIDECAR:
            return b""
        if len(frame) % (self._sidecar_channels * 2) != 0:
            self._counters["sidecar_invalid_frames"] += 1
            return b""
        self._counters["sidecar_frames"] += 1
        return self._sidecar_specsub.feed(frame)

    def _mixer_for(self, mode: str) -> ChannelDownmixer | None:
        if mode not in (MODE_STRONGEST, MODE_FIXED):
            return None
        mixer = self._mixers.get(mode)
        if mixer is None:
            mixer = ChannelDownmixer(
                channels=self.channels,
                strategy=mode,
                fixed_channel=self.fixed_channel,
                reference_channels=self.reference_channels,
            )
            self._mixers[mode] = mixer
        return mixer

    def set_playing(self, playing: bool) -> None:
        """由调用方按宿主播放态逐帧传入（speaker.status == "playing"）。"""
        self._playing = bool(playing)

    def feed(self, raw: bytes) -> bytes:
        """处理一帧多声道交错 int16 PCM → 1ch int16（可能为空串：残留未满帧）。"""
        if not raw or not self.active or self.channels < 2:
            return b""
        self._counters["frames_in"] += 1
        playing = getattr(self, "_playing", False)
        mode = self._mode_for(playing)
        if mode != self._last_mode:
            if self._last_mode is not None:
                self._counters["mode_switches"] += 1
            self._last_mode = mode

        if mode == MODE_SIDECAR:
            # sidecar 健康 ⇒ KWS 输入由 sidecar 8ch（feed_sidecar）供给；
            # client 流在此模式下不投递（近端必须与参考同源，防跨流拼接）。
            return b""
        if mode == MODE_SPECSUB and self._specsub is not None:
            out = self._specsub.feed(raw)
            st = self._specsub.stats()
            self._counters["specsub_bypass"] = (
                st.get("bypass_no_ref", 0) + st.get("bypass_ref_flat", 0))
            return out
        if mode == MODE_AEC and self._aec is not None:
            stats = self._aec.stats()
            out = self._aec.feed(raw)
            self._counters["aec_bypass"] = stats.get("blocks_bypassed", 0)
            return out

        mixer = self._mixer_for(mode)
        if mixer is None:  # inherit 兜底（不应到达：active 时 mode 非 inherit）
            return b""
        return mixer.feed(raw)

    def reset(self) -> None:
        """录音重启等场景：清掉选麦/对齐线的陈旧状态。"""
        for mixer in self._mixers.values():
            mixer.reset()
        if self._aec is not None:
            self._aec.reset()
        if self._sidecar_specsub is not None:
            self._sidecar_specsub.reset()
        self._last_mode = None

    def stats(self) -> dict:
        stats = dict(self._counters)
        stats.update(
            {
                "playing_mode": self.playing_mode,
                "idle_mode": self.idle_mode,
                "active": self.active,
                "aec_enabled": self._aec is not None,
            }
        )
        if self._aec is not None:
            stats["aec"] = self._aec.stats()
        if self._sidecar_specsub is not None:
            stats["sidecar"] = {
                "configured": True,
                "ready": self._sidecar_ready(),
                "healthy": self._sidecar_healthy,
                "channels": self._sidecar_channels,
                "fallbacks": self._counters["sidecar_fallbacks"],
                "invalid_frames": self._counters["sidecar_invalid_frames"],
            }
        return stats
