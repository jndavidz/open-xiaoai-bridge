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
非法值启动期告警并回落 "inherit"，不静默。

播放态判定使用既有宿主状态（speaker.status == "playing"），不在音频路径里
新造状态源。AEC 本体见 aec.py（默认关闭）。
"""

from __future__ import annotations

from core.services.audio.channels import (
    STRATEGY_FIXED,
    STRATEGY_STRONGEST,
    ChannelDownmixer,
)
from core.utils.logger import logger

MODE_INHERIT = "inherit"
MODE_STRONGEST = STRATEGY_STRONGEST
MODE_FIXED = STRATEGY_FIXED
MODE_AEC = "aec"
VALID_MODES = (MODE_INHERIT, MODE_STRONGEST, MODE_FIXED, MODE_AEC)

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


class WakeupInputChain:
    """按播放态选择唤醒输入通道并产出 1ch PCM（喂 GlobalStreamKws）。

    `feed(raw_bytes)` 输入与 XiaoAiService.on_input_data 相同的多声道交错
    int16 PCM；返回 1ch int16。主链（GlobalStream）的降声道不由本类触碰。
    """

    def __init__(self, config: dict, channels: int) -> None:
        parsed = parse_wakeup_channel_config(config.get("wakeup_channel"))
        self.playing_mode = parsed["playing"]
        self.idle_mode = parsed["idle"]
        self.fixed_channel = parsed["fixed_channel"]
        self.channels = int(channels)
        self._aec_params = resolve_aec_params(config.get("aec"), channels)
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
        self._last_mode = None
        self._counters = {"frames_in": 0, "mode_switches": 0, "aec_bypass": 0}

    @property
    def active(self) -> bool:
        return self.playing_mode != MODE_INHERIT or self.idle_mode != MODE_INHERIT

    @property
    def _uses_aec(self) -> bool:
        return MODE_AEC in (self.playing_mode, self.idle_mode) and self._aec_params is not None

    def _mode_for(self, playing: bool) -> str:
        mode = self.playing_mode if playing else self.idle_mode
        # aec 配置了但不可用（无参考声道）→ 静默回落固定单麦（旁路语义）
        if mode == MODE_AEC and self._aec_params is None:
            return MODE_FIXED
        return mode

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
        return stats
