"""PC 语音注入（inject）的开关判定与开窗原子性（ticket 07）。

纯逻辑模块：只依赖 stdlib，不 import aiohttp / 设备 / 会话实现，便于单测。
HTTP 适配（路由、鉴权、json_response）留在 `core/services/api_server.py`。

## 开关语义（统一）

- 配置键 `inject.enabled`（config.py 的 APP_CONFIG），**默认 false**；
  环境变量 `INJECT_ENABLE` 可强制覆盖（1/true/yes/on 开，0/false/no/off 关），
  非法值忽略、回落到配置值。优先级：env > config.py > 默认 false。
- 关闭时：
  - **不启动**数据面中继 `:9093`（APIServer.start 跳过 InjectRelay.start）；
  - 控制面写端点（`POST /api/inject`）与数据面端点（`/api/inject/stream`、
    `/api/inject/relay`、`/api/inject/text`）一律返回 **503 + code="inject_disabled"**；
  - **唯一例外**：只读状态探针 `GET /api/inject` 返回 200 + `enabled=false`
    —— 让运维能区分「模块关闭」与「窗口未开」，与 `/api/health` 的
    `inject.enabled=false` 同义；
  - `/api/health` 仍可用，并报告 `inject.enabled=false`。
- 开启时行为与既有实现一致。

## 数据面（:9093）鉴权语义（ticket 12 / notes/10 §3）

合法客户端是音箱 hook（TCP 客户端拉流），bridge 只写不读 ⇒ 攻击面是
「窃听 PCM（= PC 麦克风语音）」与「挤掉合法 hook」。鉴权模式判定：

- `INJECT_ALLOW_CIDR`（逗号分隔 CIDR，建议设备 /32）：mode=`cidr`，
  连接按源 IP 过滤，**先判定后接管**（不匹配的连接拿不到任何 PCM）；
- 未配 CIDR 但显式 `INJECT_ALLOW_ANON=1`：mode=`anon`（裸奔，仅限临时排障）；
- 其余（含 `INJECT_ENABLE=1` 但什么都没配）：mode=`refused`——
  **拒绝监听**（共享密钥首包鉴权需 hook C 改动，未实施前不允许无鉴权裸跑）；
  CIDR 配了但解析非法同样 `refused`（fail closed，不悄悄降级成 anon）。
  共享密钥首包（`INJECT_AUTH_KEY`）留待 hook 改动落地后以 mode=`key` 接入。
启动日志固定一行 `inject data plane: auth=<mode>`，让「当前到底有没有鉴权」不靠猜。

## 开窗原子性

开窗 = 闸门 + 数据面中继 + 引擎唤醒三者同开同关。顺序固定为
**开闸门 → 开中继 → 唤醒引擎**；任一步失败即整窗回滚（关闸门 + 关中继），
不留下「闸门开着但引擎没醒」或「中继开着但闸门关」的半开状态。
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass

# 环境变量覆盖名（与配置键 `inject.enabled` 并存）
ENV_INJECT_ENABLE = "INJECT_ENABLE"
# 数据面鉴权（ticket 12）：设备 IP 白名单 / 显式裸奔许可
ENV_INJECT_ALLOW_CIDR = "INJECT_ALLOW_CIDR"
ENV_INJECT_ALLOW_ANON = "INJECT_ALLOW_ANON"

# 关闭语义的固定错误码/文案（端点与测试共用，避免各处自造字符串）
INJECT_DISABLED_CODE = "inject_disabled"
INJECT_DISABLED_ERROR = (
    "PC 语音注入模块已关闭（配置 inject.enabled=false，或环境变量 INJECT_ENABLE=0）"
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _flag(raw) -> bool | None:
    """把 1/true/yes/on、0/false/no/off 解析为 bool；其它（含空）返回 None。"""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None


def _config_enabled(config) -> bool:
    """从 ConfigManager / 嵌套 dict 读取 `inject.enabled`；异常一律按关闭处理。"""
    if config is None:
        return False
    try:
        if hasattr(config, "get_app_config"):
            value = config.get_app_config("inject.enabled", False)
        elif isinstance(config, Mapping):
            section = config.get("inject") or {}
            value = section.get("enabled", False) if isinstance(section, Mapping) else False
        else:
            return False
    except Exception:
        return False
    if isinstance(value, str):
        return _flag(value) is True
    return bool(value)


def resolve_inject_enabled(config=None, env: Mapping[str, str] | None = None) -> bool:
    """判定注入模块是否启用（唯一判定点）。

    Args:
        config: ConfigManager 实例或 `APP_CONFIG` 形式的嵌套 dict。
        env: 环境映射（默认 `os.environ`），用于 `INJECT_ENABLE` 覆盖。
    """
    environ = os.environ if env is None else env
    override = _flag(environ.get(ENV_INJECT_ENABLE))
    if override is not None:
        return override
    return _config_enabled(config)


@dataclass(frozen=True)
class InjectDataAuth:
    """:9093 数据面的鉴权模式判定结果（`resolve_inject_data_auth` 的产物）。

    mode 取值：`cidr`（按 networks 过滤源 IP）/ `anon`（显式裸奔）/
    `refused`（拒绝监听）。`reason` 仅在 refused 时有人读原因（不含敏感值）。
    """

    mode: str
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    reason: str = ""


def parse_inject_cidrs(
    raw: str,
) -> tuple[
    tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    tuple[str, ...],
]:
    """解析逗号分隔 CIDR 列表 → (networks, errors)。

    `strict=False`：`10.10.10.20/24` 这类带主机位的输入按掩码归零接受
    （只会收窄到所写前缀，不会放大）；语法非法才进 errors。
    """
    networks = []
    errors: list[str] = []
    for index, part in enumerate(str(raw or "").split(","), start=1):
        value = part.strip()
        if not value:
            errors.append(f"条目 {index} 为空")
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            errors.append(f"条目 {index}（{value}）不是合法 CIDR")
    return tuple(networks), tuple(errors)


def peer_allowed(
    peer_ip: str | None,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    """源 IP 是否命中任一允许网段；无网段/非法 IP 一律 False（fail closed）。

    IPv4-mapped IPv6（`::ffff:10.x.x.x`）归一化成 IPv4 再比对。
    """
    if not peer_ip or not networks:
        return False
    try:
        ip = ipaddress.ip_address(str(peer_ip).strip())
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks)


def resolve_inject_data_auth(env: Mapping[str, str] | None = None) -> InjectDataAuth:
    """判定 :9093 数据面鉴权模式（唯一判定点，见模块文档「数据面鉴权语义」）。"""
    environ = os.environ if env is None else env
    raw_cidr = str(environ.get(ENV_INJECT_ALLOW_CIDR, "") or "").strip()
    if raw_cidr:
        networks, errors = parse_inject_cidrs(raw_cidr)
        if errors:
            return InjectDataAuth(
                mode="refused",
                reason=f"INJECT_ALLOW_CIDR 非法（{'; '.join(errors)}），fail closed",
            )
        return InjectDataAuth(mode="cidr", networks=networks)
    if _flag(environ.get(ENV_INJECT_ALLOW_ANON)) is True:
        return InjectDataAuth(mode="anon")
    return InjectDataAuth(
        mode="refused",
        reason=(
            "未配置 INJECT_ALLOW_CIDR（共享密钥首包鉴权未实施），"
            "也未显式 INJECT_ALLOW_ANON=1：拒绝无鉴权监听"
        ),
    )


@dataclass(frozen=True)
class WindowResult:
    """一次开窗尝试的结果（成功或已回滚的失败）。"""

    ok: bool
    gate: str                     # "open" / "closed"
    remaining: float = 0.0        # 闸门剩余秒数
    ttl: float | None = None      # 本次闸门有效期（秒）
    wakeup: bool | None = None    # speaker.wake_up 的返回值
    code: str = ""                # 失败时的错误码
    error: str = ""               # 失败时的说明（不含凭证/文本）
    rolled_back: bool = False     # 失败时是否已完成回滚


def _close_quietly(events, relay) -> None:
    """回滚：关闸门 + 关中继。任一步异常都不能掩盖原始失败。"""
    try:
        events.close_injection_gate()
    except Exception:
        pass
    if relay is not None:
        try:
            relay.close_session("open window rollback")
        except Exception:
            pass


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def open_injection_window(events, relay, speaker, duration: float | None = None) -> WindowResult:
    """原子开窗：开闸门 → 开中继 → 唤醒引擎；任一步失败整窗回滚。

    Args:
        events: `WakeupSessionManager`（或测试替身），需提供 open/close/remaining。
        relay: `InjectRelay`（或测试替身），需提供 open_session/close_session。
        speaker: `SpeakerManager`（或测试替身），需提供 `wake_up(awake, silent)`。
        duration: 闸门有效期（秒）；None = 使用 EventManager 默认 TTL。

    Returns:
        WindowResult：失败时 `ok=False`，且闸门/中继均已回滚（`rolled_back=True`）。
    """
    try:
        ttl = events.open_injection_gate(duration)
    except Exception as exc:
        _close_quietly(events, relay)
        return WindowResult(
            ok=False, gate="closed", code="gate_open_failed",
            error=_error_text(exc), rolled_back=True,
        )

    if relay is None:
        _close_quietly(events, relay)
        return WindowResult(
            ok=False, gate="closed", ttl=ttl, code="relay_unavailable",
            error="inject relay not initialized", rolled_back=True,
        )
    try:
        relay.open_session()
    except Exception as exc:
        _close_quietly(events, relay)
        return WindowResult(
            ok=False, gate="closed", ttl=ttl, code="relay_open_failed",
            error=_error_text(exc), rolled_back=True,
        )

    if speaker is None:
        _close_quietly(events, relay)
        return WindowResult(
            ok=False, gate="closed", ttl=ttl, code="speaker_unavailable",
            error="speaker not initialized", rolled_back=True,
        )

    try:
        woken = await speaker.wake_up(awake=True, silent=True)
    except Exception as exc:
        _close_quietly(events, relay)
        return WindowResult(
            ok=False, gate="closed", ttl=ttl, code="wake_failed",
            error=_error_text(exc), rolled_back=True,
        )
    # 引擎没醒 ⇒ 云端 ASR 不会出流，窗口没有意义：按失败整窗回滚。
    if woken is False:
        _close_quietly(events, relay)
        return WindowResult(
            ok=False, gate="closed", ttl=ttl, wakeup=False, code="wake_failed",
            error="speaker.wake_up returned False", rolled_back=True,
        )

    try:
        remaining = events.injection_gate_remaining()
    except Exception:
        remaining = 0.0
    return WindowResult(
        ok=True, gate="open", remaining=remaining, ttl=ttl, wakeup=woken,
    )
