"""设备能力表与禁区白名单。

这是 bridge 唯一的设备方法登记处：方法名、参数形状、成功判据、回读方法、
超时、是否阻塞、是否幂等都在这里声明。业务代码只调 `DeviceCapabilities`
的具名方法，不再自己拼 ubus / shell 字符串。

禁区（`FORBIDDEN_METHODS`）不进入能力表；`resolve_capability()` 与
`ensure_script_allowed()` 两道闸门保证它们无法经能力层调用。

⚠️ 禁区闸门是**防误用**，不是安全沙箱：`shell` 能力本身等价设备 root，
且正则无法覆盖所有 shell 引号/转义写法。绝不可把外部输入直接传给能力层。

依据：bridge/AGENTS.md「设备能力扩展面」与 docs/devices/lx06/ 的参数契约。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class DeviceCapabilityError(Exception):
    """能力层基类异常。"""


class UnknownCapabilityError(DeviceCapabilityError):
    """请求了未登记的能力。"""


class ForbiddenCapabilityError(DeviceCapabilityError):
    """请求了禁区方法（不允许进入能力层）。"""


class SuccessCriterion(str, Enum):
    """成功判据。"""

    JSON_CODE = "json_code"  # 解析 stdout JSON 的 code 字段 == 0
    JSON_PARSED = "json_parsed"  # 解析 stdout 为 JSON 对象即可（无 code 外层）
    JSON_FIELD = "json_field"  # 解析后必须存在指定字段
    EXIT_CODE = "exit_code"  # 成功时无响应体 / 非 JSON ⇒ 只看退出码
    EXIT_CODE_IN = "exit_code_in"  # 退出码在 expectation 列出的集合内（成功码不止一个）
    STDOUT_CONTAINS = "stdout_contains"  # 多行输出 / 纯文本结果
    READBACK = "readback"  # 写操作：只以回读判定，返回码不构成失败判据


class CapabilityGroup(str, Enum):
    """能力分三组（spec §设备能力层）。"""

    READ = "read"  # 只读查询
    WRITE = "write"  # 可回读的写操作（必须登记 readback）
    ACTION = "action"  # 仅执行一次的副作用操作（不保证可回读）


@dataclass(frozen=True)
class ParamSpec:
    """参数形状。"""

    name: str
    type: str  # "str" | "int" | "bool"
    required: bool = True
    description: str = ""


@dataclass(frozen=True)
class CapabilitySpec:
    """一条能力登记项。"""

    name: str
    method: str  # 设备侧入口，例如 "ubus:mediaplayer.player_set_volume" / "shell:...".
    params: tuple[ParamSpec, ...]
    success: SuccessCriterion
    group: CapabilityGroup
    readback: str | None  # 回读能力名（写操作必填）
    timeout_ms: float  # 默认超时；阻塞播报允许按音频时长放宽
    blocking: bool  # 是否阻塞到设备侧动作完成（如 miplayer 播放整段音频）
    idempotent: bool
    expectation: str | None = None  # 成功判据的期望值（字段路径 / 子串）
    description: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)


def _p(name: str, type_: str, required: bool = True, desc: str = "") -> ParamSpec:
    return ParamSpec(name=name, type=type_, required=required, description=desc)


# 阻塞播报按音频时长放宽（调用方也会显式传 timeout）；其余默认 ≤6 秒。
_BLOCKING_PLAY_TIMEOUT_MS = 600_000

CAPABILITIES: dict[str, CapabilitySpec] = {
    # ---- 只读查询 ----
    "mic_get": CapabilitySpec(
        name="mic_get",
        method="shell:[ ! -f /tmp/mipns/mute ] && echo on || echo off",
        params=(),
        # 命令两个分支都以 echo 结束（退出码 0）；on/off 由 stdout 区分。
        # 不能在登记项里写 expectation="on"，否则合法的 off 会被判为失败。
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="读麦克风是否开启（/tmp/mipns/mute 不存在 ⇒ on）",
    ),
    "volume_get": CapabilitySpec(
        name="volume_get",
        method="ubus:mediaplayer.player_get_context",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="读当前音量（player_get_context.info.volume，百分比语义）",
        notes=(
            "不要用 get_media_volume（25-255 非线性百分比，与 set 通道不同量纲）",
            "不要用 amixer（与播放器音量完全无关，恒 85%）",
        ),
    ),
    "player_context": CapabilitySpec(
        name="player_context",
        method="ubus:mediaplayer.player_get_context",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="播放上下文回读（status/volume/player_mode 等 9 字段）",
    ),
    "playback_status": CapabilitySpec(
        name="playback_status",
        method="mphelper mute_stat",
        params=(),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="播放状态（mphelper mute_stat，1=播放中 / 2=暂停）",
    ),
    "service_status": CapabilitySpec(
        name="service_status",
        method="ubus:service.list",
        params=(_p("name", "str", False, "procd 服务名，默认 mediaplayer"),),
        success=SuccessCriterion.JSON_PARSED,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="procd 服务存活（running/pid/respawn）",
    ),
    "system_memory": CapabilitySpec(
        name="system_memory",
        method="ubus:system.info",
        params=(),
        success=SuccessCriterion.JSON_PARSED,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="系统内存（memory.free；118MB 无 swap，健康探针）",
    ),
    "tts_file_exists": CapabilitySpec(
        name="tts_file_exists",
        method="shell:test -f <path>",
        params=(_p("path", "str", True, "设备本地文件路径"),),
        success=SuccessCriterion.STDOUT_CONTAINS,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        expectation="yes",
        description="回读 TTS 合成产物是否真的落盘",
    ),
    "tts_cleanup": CapabilitySpec(
        name="tts_cleanup",
        method="shell:rm -f <path>",
        params=(_p("path", "str", True, "TTS 临时文件路径（仅 /tmp/tts/ 前缀）"),),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="删除 TTS 合成临时文件（前缀不是 /tmp/tts/ 时拒绝执行）",
        notes=("路径来自 text_to_speech 的 info.path，删前必须校验前缀",),
    ),
    "device_info": CapabilitySpec(
        name="device_info",
        method="shell:micocfg_model + micocfg_sn",
        params=(),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="设备型号与序列号",
    ),
    "boot_partition_get": CapabilitySpec(
        name="boot_partition_get",
        method="shell:fw_env -g boot_part",
        params=(),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="读启动分区",
    ),
    "nightmode_current": CapabilitySpec(
        name="nightmode_current",
        method="ubus:nightmode.current",
        params=(),
        success=SuccessCriterion.JSON_PARSED,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="夜间模式实时状态（current：此刻是否处于夜间时段）",
        notes=(
            "current=实时生效 vs status=配置（两接口语义不同，判定用 current）",
            "音量护栏只看 volume 字段（total/light 无关）——设备真值见 "
            "ws: docs/devices/lx06/nightmode-volume-policy.md §2",
        ),
    ),
    "firmware_info": CapabilitySpec(
        name="firmware_info",
        method="shell:micocfg_<field> 白名单单字段组合",
        params=(),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="固件健康信息（版本/构建时间/板型/WiFi 芯片/工作模式）",
        notes=(
            "micocfg 单字段命令天然满足 PII 边界（无 %-16s 前缀、不误带凭证）——"
            "事实层 ws: docs/devices/lx06/micocfg-device-info-cli.md §8",
            "白名单只取无敏感标记的 getter；sn/mac/ssid/bssid/channel_secret 等"
            "🔴 字段与全部 micocfg_* setter 绝不调用（setter 已入脚本禁区闸门）",
        ),
    ),
    # ---- LED（A-6/P3 票 02：灯语可视化） ----
    "led_set_pixel": CapabilitySpec(
        name="led_set_pixel",
        method="shell:echo \"<i> 0x<RGB>\" > /sys/devices/i2c-0/0-003a/led_rgb",
        params=(
            _p("index", "int", True, "灯位 0-17（AW20054 共 18 灯）"),
            _p("rgb", "str", True, "24bit RGB 十六进制，如 ff0000"),
        ),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="sysfs 单灯直写（任意颜色静态定格）",
        notes=(
            "驱动校验生效：格式错会打 dmesg 且非零退出，rc=0 即真成功——"
            "事实层 ws: docs/devices/lx06/led-control-api.md §1",
            "与 led show 动画互抢占（共用 ledserver 只写 fd）：同一时段只用一条；"
            "用完写 0x000000 归位，不要用 shut_all 清 bridge 自己的灯",
            "任何效果用完必须清除（LED 是用户可见状态）",
        ),
    ),
    "led_effect": CapabilitySpec(
        name="led_effect",
        method="ubus:led.show",
        params=(_p("effect", "int", True, "原厂动画 ID（白名单 0-7/9-16）"),),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="原厂预设动画（16 种，经 ledserver）",
        notes=(
            "白名单 0-7/9-16：L=8 是 RGB 入口但实测不生效（停在 start_show），"
            "≥17 无效果函数——事实层 led-control-api.md §2/§3",
            "播放自动带动 L=3/L=12（player 自己用）——灯语播放态不干预",
        ),
    ),
    "led_clear_effect": CapabilitySpec(
        name="led_clear_effect",
        method="ubus:led.shut_all",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="停全部原厂动画并全灭（收尾用；会清系统效果，慎用）",
    ),
    "led_status": CapabilitySpec(
        name="led_status",
        method="ubus:led.status",
        params=(),
        success=SuccessCriterion.JSON_PARSED,
        group=CapabilityGroup.READ,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="当前效果表（32 项 {type,pos,rgb}）",
        notes=(
            "夜间返回空 info（nightmode 门控，仅查询受门控、show 不受）——"
            "读不到不等于失败，判灭不要依赖本表（led_position 也误导）",
        ),
    ),
    # ---- 红外（A-7/P3 票 04：首个写操作工具实战，C 级护栏） ----
    "ir_send": CapabilitySpec(
        name="ir_send",
        method="shell:echo \"<seq>\" > /sys/ir_tx_gpio/ir_data",
        params=(_p("pattern", "str", True, "逗号分隔十进制 µs 序列（偶数项）"),),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=True,
        idempotent=False,
        description="红外发射（sysfs 直写，gpio-ir-tx 内核软件 PWM 37.9kHz）",
        notes=(
            "时序语义：偶数索引=载波（val/26.4 周期）、奇数索引=静默 µs——"
            "事实层 ws: docs/devices/lx06/kernel-surface.md §4；",
            "🔴 发送期间内核关中断（raw_spin_lock_irqsave 忙等）：一条 NEC 帧"
            "≈67ms 关中断 ⇒ 必须串行 + 限长 + 不与播放/TTS 并发（否则音频 XRUN）",
            "码值库外部提供（config ir.codes）；learn 录制属设备写、单独授权，不默认",
            "cat 该节点报 I/O error 是设计（show=NULL），不是故障",
        ),
    ),
    # ---- 可回读的写操作 ----
    "mic_set": CapabilitySpec(
        name="mic_set",
        method="ubus:pnshelper.event_notify",
        params=(_p("muted", "bool", True, "True=静音(event:7)，False=解除静音(event:8)"),),
        success=SuccessCriterion.READBACK,
        group=CapabilityGroup.WRITE,
        readback="mic_get",
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="开关麦克风静音（设备真值：event:7 建 mute 文件，event:8 删）",
        notes=(
            "event:8 在麦已开时返回 code:-1，属幂等语义，不看返回码",
            "成败只看 mic_get 回读",
        ),
    ),
    "mic_clear_mute_flag": CapabilitySpec(
        name="mic_clear_mute_flag",
        method="shell:rm -f /tmp/mipns/mute",
        params=(),
        success=SuccessCriterion.READBACK,
        group=CapabilityGroup.WRITE,
        readback="mic_get",
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="清掉宿主侧静音标志（T7.6 解除停止聆听的兜底）",
    ),
    "volume_set": CapabilitySpec(
        name="volume_set",
        method="ubus:mediaplayer.player_set_continuous_volume",
        params=(_p("level", "int", True, "百分比音量 0-100"),),
        success=SuccessCriterion.READBACK,
        group=CapabilityGroup.WRITE,
        readback="volume_get",
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="设音量（百分比通道，回读 player_get_context.info.volume）",
        notes=(
            "player_set_continuous_volume 按百分比直落，不走档位映射",
            "player_set_volume 是档位口径且 unit/type 无法切换量纲，不要用",
        ),
    ),
    "boot_partition_set": CapabilitySpec(
        name="boot_partition_set",
        method="shell:fw_env -s boot_part",
        params=(_p("partition", "str", True, "boot0 / boot1"),),
        success=SuccessCriterion.READBACK,
        group=CapabilityGroup.WRITE,
        readback="boot_partition_get",
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="切启动分区（以 fw_env -g 回读为准）",
    ),
    # ---- 副作用操作（播放 / 服务控制） ----
    "play_url": CapabilitySpec(
        name="play_url",
        method="ubus:mediaplayer.player_play_url",
        params=(_p("url", "str", True, "音频链接"),),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback="player_context",
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="非阻塞播放 URL（媒体播放器通道）",
        notes=("code:0 不等于播放成功，回读 player_get_context",),
    ),
    "play_url_blocking": CapabilitySpec(
        name="play_url_blocking",
        method="shell:miplayer -f <url>",
        params=(_p("url", "str", True, "音频链接"),),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=_BLOCKING_PLAY_TIMEOUT_MS,
        blocking=True,
        idempotent=False,
        description="阻塞播放 URL（miplayer，成功时无 JSON 体）",
    ),
    "play_file": CapabilitySpec(
        name="play_file",
        method="shell:miplayer -f <path>",
        params=(_p("path", "str", True, "设备本地音频文件路径"),),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=_BLOCKING_PLAY_TIMEOUT_MS,
        blocking=True,
        idempotent=False,
        description="阻塞播放设备本地文件（miplayer，无完成事件）",
    ),
    "play_text_blocking": CapabilitySpec(
        name="play_text_blocking",
        method="shell:tts_play.sh <text>",
        params=(_p("text", "str", True, "TTS 文本"),),
        success=SuccessCriterion.EXIT_CODE_IN,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=_BLOCKING_PLAY_TIMEOUT_MS,
        blocking=True,
        idempotent=False,
        expectation="0,1",
        description="阻塞 TTS 播报兜底（原厂 tts_play.sh）",
        notes=(
            "兜底路径：仅在合成落盘失败时使用，默认路径是合成落盘 + player_play_url",
            "参数必须先 JSON 转义、再 shell 转义（tts_play.sh 会把 $1 直接插进 JSON）",
            "设备真值：tts_play.sh 成功时 rc 也可能是 1 ⇒ 判据是 rc∈{0,1}；"
            "stderr 出现 'Failed to parse' 是明确的失败（client 侧叠加判定）",
        ),
    ),
    "tts_speak": CapabilitySpec(
        name="tts_speak",
        method="ubus:mibrain.text_to_speech",
        params=(_p("text", "str", True, "TTS 文本"),),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="非阻塞 TTS 播报（save:0）",
    ),
    "tts_synthesize": CapabilitySpec(
        name="tts_synthesize",
        method="ubus:mibrain.text_to_speech",
        params=(_p("text", "str", True, "TTS 文本"),),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback="tts_file_exists",
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="TTS 合成落盘（save:1, play:0），回读设备文件是否存在",
        notes=("play:0 必须显式传，否则会直接播放",),
    ),
    "pause": CapabilitySpec(
        name="pause",
        method="mphelper pause",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback="player_context",
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="暂停当前播放（mphelper pause）",
    ),
    "playback_resume": CapabilitySpec(
        name="playback_resume",
        method="mphelper play",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="继续播放（mphelper play）",
        notes=("播放启动有延迟，不做立即回读；状态由 playback_status 轮询",),
    ),
    # next/prev 用 iot_ 变体：原厂 next/prev 会先播 key_prev_next.opus 提示音，
    # 两段 JSON 混在 stdout 会让 JSON_CODE 判据失效；iot_ 变体无提示音、无埋点，
    # stdout 只剩 player_play_operation 的 JSON。脚本原文见
    # ws: docs/devices/lx06/mphelper-playback-cli.md §1。
    "playback_next": CapabilitySpec(
        name="playback_next",
        method="mphelper iot_next",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="切到下一首（设备 mediaplayer 队列，mphelper iot_next）",
        notes=(
            "只对设备自有播放队列有效（原生小爱/媒体播放器）；DLNA 投送中队列归投送方，"
            "此时 next 通常无效且报文无法区分",
            "✅ 真机已验证（2026-10-28）：`code:0` 与实际切曲一致（原生小爱歌单，"
            "`[SongRequest] switched device queue`）",
        ),
    ),
    "playback_prev": CapabilitySpec(
        name="playback_prev",
        method="mphelper iot_prev",
        params=(),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="切到上一首（设备 mediaplayer 队列，mphelper iot_prev）",
        notes=(
            "与 playback_next 同口径：仅设备自有队列有效，DLNA 投送中无效",
            "next 已真机验证（2026-10-28）；prev 走同一判据与路径，未单独实测",
        ),
    ),
    "stop_audio": CapabilitySpec(
        name="stop_audio",
        method="shell:killall tts_play.sh miplayer + mphelper pause",
        params=(),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=True,
        description="停设备侧阻塞/非阻塞播报进程（不含 PCM 通道）",
    ),
    "wake": CapabilitySpec(
        name="wake",
        method="ubus:pnshelper.event_notify",
        params=(_p("silent", "bool", False, "是否静默唤醒"),),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="唤醒小爱（src 1/0, event 0）",
    ),
    "sleep": CapabilitySpec(
        name="sleep",
        method="ubus:pnshelper.event_notify (7 -> 8)",
        params=(),
        success=SuccessCriterion.STDOUT_CONTAINS,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        expectation='"code": 0',
        description="休眠小爱（先 event:7 再 event:8，多行输出无单一 JSON）",
    ),
    "ask_xiaoai": CapabilitySpec(
        name="ask_xiaoai",
        method="ubus:mibrain.ai_service",
        params=(
            _p("text", "str", True, "交给原生小爱的文本指令"),
            _p("silent", "bool", False, "是否静默执行"),
        ),
        success=SuccessCriterion.JSON_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="把文字指令交给原生小爱执行",
    ),
    "restart_xiaoai_service": CapabilitySpec(
        name="restart_xiaoai_service",
        method="shell:/etc/init.d/mico_aivs_lab restart",
        params=(),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="重启小爱语音服务（不可用中断方式，仅保留历史入口）",
    ),
    "shell": CapabilitySpec(
        name="shell",
        method="shell:run_shell",
        params=(
            _p("script", "str", True, "设备侧 shell 脚本"),
            _p("timeout_ms", "int", False, "超时毫秒，默认 10000"),
        ),
        success=SuccessCriterion.EXIT_CODE,
        group=CapabilityGroup.ACTION,
        readback=None,
        timeout_ms=6000,
        blocking=False,
        idempotent=False,
        description="通用 shell 执行（仍受禁区脚本闸门约束）",
    ),
}


# 禁区：不进入能力表，也不能被 resolve / shell 触达。
# 依据：bridge/AGENTS.md「禁止施工 / 禁止暴露」与 docs/devices/lx06/README.md。
FORBIDDEN_METHODS: tuple[tuple[str, str], ...] = (
    ("miio", "miio 全对象（MIoT 控制面，通用转口可读写设备身份）"),
    ("messagingagent.identify_device", "messagingagent 设备识别（改设备状态）"),
    ("messagingagent.set_kv", "messagingagent 写 KV（改云端配置）"),
    ("service.set", "procd service 写组（可起停任意服务，可变砖）"),
    ("service.add", "procd service 写组"),
    ("service.delete", "procd service 写组"),
    ("service.signal", "procd service 写组"),
    ("service.update", "procd service 写组"),
    ("system.reboot", "system 重启"),
    ("system.sysupgrade", "system 升级"),
    ("system.watchdog", "system 看门狗"),
    ("player_event", "player_event（写播放事件总线）"),
    ("notify", "notify 整对象（假成功通道，create 不入队/不发声）"),
    ("mediaplayer.set_dlna_status", "DLNA 开启（局域网无鉴权可控）"),
)

_FORBIDDEN_SCRIPT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcall\s+miio\b"), "miio 全对象"),
    (
        re.compile(r"\bcall\s+messagingagent\s+(?:identify_device|set_kv)\b"),
        "messagingagent 写方法",
    ),
    (
        re.compile(r"\bcall\s+service\s+(?:set|add|delete|signal|update)"),
        "procd service 写组",
    ),
    (re.compile(r"\bcall\s+system\s+(?:reboot|sysupgrade|watchdog)\b"), "system 危险方法"),
    (re.compile(r"\bplayer_event\b"), "player_event"),
    (re.compile(r"\bcall\s+notify\b"), "notify 整对象"),
    (re.compile(r"\bcall\s+mediaplayer\s+set_dlna_status\b"), "DLNA 开启"),
    # micocfg setter 组（任意配置写/配网/改状态）——P3 票 01 起入闸门；
    # 只读 getter（micocfg_version 等）不受影响（事实层 micocfg §8.2）。
    (
        re.compile(r"\bmicocfg_(?:[a-z0-9_]*set|miio_register|miio_wifi_(?:connected|disconnected)|wifi_scan|wifi_p2p|light_show|light_shut)\b"),
        "micocfg setter/动作组（任意配置写入口，最高危）",
    ),
)


def _matches_forbidden_name(name: str) -> str | None:
    for prefix, reason in FORBIDDEN_METHODS:
        if (
            name == prefix
            or name.startswith(prefix + ".")
            or name.startswith(prefix + "_")
        ):
            return reason
    return None


def capability_table() -> dict[str, CapabilitySpec]:
    """能力表（只读快照，供健康检查 / 文档 / 测试使用）。"""

    return dict(CAPABILITIES)


def resolve_capability(name: str) -> CapabilitySpec:
    """按名字取能力登记项；禁区与未登记都拒绝。"""

    reason = _matches_forbidden_name(name)
    if reason is not None:
        raise ForbiddenCapabilityError(f"{name} 是禁区方法：{reason}")
    spec = CAPABILITIES.get(name)
    if spec is None:
        raise UnknownCapabilityError(f"未登记的设备能力：{name}")
    return spec


def ensure_script_allowed(script: str) -> None:
    """拒绝包含禁区对象的 shell 脚本（能力层最后一道闸门）。"""

    for pattern, reason in _FORBIDDEN_SCRIPT_PATTERNS:
        if pattern.search(script):
            raise ForbiddenCapabilityError(f"脚本触达禁区（{reason}）：{script}")
