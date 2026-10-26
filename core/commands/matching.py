"""指令表匹配的归一化与别名键解析（B-31，评审 W4 的无行为变化底座）。

背景（deploy/config.py §XIAOAI_COMMANDS 实测记录）：原生 ASR 对转写不稳——
「打开Kodi→打开CODY」「HiFi模式→哈哈模式」，且「打开Kodi」曾转成「打开电脑」
误命中。本模块提供两级防御：

1. **归一化**（无条件启用）：两侧（文本与表键）做同口径正规化——全角→半角、
   去空白标点、拉丁字母小写。对现有全表是恒等映射（键与命中文本同步正规化，
   语义逐字等价），但对「Kodi/kodi/KODI」「带尾随空格」这类转写抖动即刻免疫。
2. **别名键**（数据驱动，表空 = 现状）：`{别名: 正式键}`，命中的别名映射到
   正式键。**不含模糊/拼音匹配**——那会把「没说也乱触发」引入指令面
   （宁可少截不可错吞，deploy/config.py §边界规则），拼音级容错的放行范围
   属用户裁决（ws: pending-followups §5），本模块只留机制不填数据。
"""

import re
import unicodedata

# 去除类：空白与常见中英标点（ASR 输出的标点噪声）
_STRIP_RE = re.compile(r"[\s，。！？、；：\"'“”‘’（）()\[\]【】,.\!?;:~～…—·]+")


def _strip_noise(text: str) -> str:
    return _STRIP_RE.sub("", text)


def normalize_command_text(text: str) -> str:
    """指令匹配口径的正规化：NFKC 全角→半角 + 去空白标点 + 拉丁小写。"""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return _strip_noise(normalized).lower()


def resolve_command_key(
    text: str,
    table: dict,
    aliases: dict[str, str] | None = None,
) -> str | None:
    """文本 → 指令表键（归一化精确命中 → 别名映射）；未命中返回 None。

    别名值必须真实存在于 table（悬空别名忽略并告警一次语义由调用方日志承担，
    本函数静默跳过——别名是部署数据，脏数据不该炸路由）。
    """
    normalized = normalize_command_text(text)
    if not normalized or not table:
        return None

    for key in table:
        if normalize_command_text(key) == normalized:
            return key

    for alias, canonical in (aliases or {}).items():
        if normalize_command_text(alias) == normalized and canonical in table:
            return canonical
    return None
