"""审计用「调用方标识」解析（纯函数，不 import aiohttp / core 其它模块，便于单测）。

背景（ticket 09，来源 `notes/07-security-caller-inventory.md` P1）：
    原实现**无条件**信任 `X-Forwarded-For` / `X-Real-IP`，但生产环境**没有**
    反向代理（host 网络 + 0.0.0.0 直连）⇒ 任何局域网客户端都能自带该头伪造
    审计日志里的调用方，让「是谁让音箱说话」失去意义。

语义：
    - 默认（`TRUST_PROXY` 未配置）只认 TCP peer（aiohttp 的 `request.remote`）。
      该值来自连接本身，客户端无法伪造。
    - 仅在显式 `TRUST_PROXY=1`（部署在受控反代之后）时才读
      `X-Forwarded-For`（取**最右一跳**，即最近受信代理追加的值）/ `X-Real-IP`。
      取最左是客户端可伪造的值（ticket 10 §4）。
    - 头部存在却不可信时**不写日志告警**：避免攻击者刷日志；
      运维用 `/api/health` 与网络拓扑判断是否需要开 `TRUST_PROXY`。
"""

from __future__ import annotations

import os
from collections.abc import Mapping

ENV_TRUST_PROXY = "TRUST_PROXY"

_PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP")
_TRUTHY = ("1", "true", "yes", "on")


def trust_proxy_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """`TRUST_PROXY` 是否为真值；未配置/空白/其它值一律 False（默认不信任代理头）。"""
    env = os.environ if environ is None else environ
    return str(env.get(ENV_TRUST_PROXY, "") or "").strip().lower() in _TRUTHY


def resolve_caller_tag(
    remote: str | None,
    headers: Mapping[str, str] | None = None,
    *,
    trust_proxy: bool = False,
) -> str:
    """返回审计日志里的调用方标识。

    `remote` 用 aiohttp `request.remote`（TCP peer 地址），不是
    `transport.get_extra_info("peername")` —— 前者在 Unix socket 等场景下更稳定，
    且不会把端口掺进日志口径。

    当 `trust_proxy=True` 时，`X-Forwarded-For` 取**最右**一跳（最近受信代理
    追加的值）；取最左是客户端可伪造的字符串。该值仅在受控反代后面才有意义。
    """
    if trust_proxy and headers:
        for name in _PROXY_HEADERS:
            value = headers.get(name)
            if value:
                last = str(value).split(",")[-1].strip()
                if last:
                    return last
    peer = str(remote or "").strip()
    return peer or "unknown"
