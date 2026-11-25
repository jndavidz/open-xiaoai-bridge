"""CCH 布尔开关解析单一实现（2026-11-25 架构统一）。

此前 cch_cast.cast_enabled/np_enabled/m5_enabled、cch_click.enabled、
cch_fav_sync.enabled 各写一份「字符串 bool 解析」模板（Standards #G）。
本模块只收敛**解析**；配置读取仍由各模块自己的 _cfg 负责（保持测试 seam）。
"""
from __future__ import annotations


def cfg_bool(val, default: bool) -> bool:
    """把配置值解析为 bool：字符串按 1/true/yes/on 解析，其它取 bool()。"""
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    if val is None:
        return bool(default)
    return bool(val)
