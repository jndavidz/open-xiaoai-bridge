"""CCH 配置读取/布尔解析单一实现（2026-11-25 架构统一，P5 归并 _cfg）。

此前 cch_cast.cast_enabled/np_enabled/m5_enabled、cch_click.enabled、
cch_fav_sync.enabled 各写一份「字符串 bool 解析」模板（Standards #G）；
三个模块还各写一份同构 `_cfg`（读 song_request 配置段）。本模块收敛两者：
`cfg()` = 唯一配置读取 seam，`cfg_bool()` = 唯一布尔解析。
"""
from __future__ import annotations


def cfg(key: str, default=None):
    """读 song_request 配置段（与 song_bridge 同源）——唯一的 CCH 配置读取 seam。"""
    from core.tools import song_request as sr

    return sr._cfg(key, default)


def cfg_bool(val, default: bool) -> bool:
    """把配置值解析为 bool：字符串按 1/true/yes/on 解析，其它取 bool()。"""
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    if val is None:
        return bool(default)
    return bool(val)
