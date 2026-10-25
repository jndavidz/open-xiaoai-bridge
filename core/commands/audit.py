"""词表 ⊇ 指令表键 启动校验（C5/M-4，P2 ticket 02）。

免唤醒指令表加了新短语但忘了进 KWS 词表时，音箱说破嘴也不会触发——
词表与指令表此前靠人脑同步（M-4，冻结点 24 词 vs 24 键手工对齐）。
本模块开机（MainApp.run）与配置热重载（reload listener）各对账一次：
缺失输出含全部键名的 WARN，**只告警不阻断**（家庭可用性优先）。

对账口径（为什么只查 DIRECT_COMMANDS）：
  - DIRECT_COMMANDS 走 KWS 免唤醒路由：短语必须先被 KWS 离线模型认出
    （即在 wakeup.keywords 总词表里）才到得了 router 的表查询 → 必须覆盖；
  - XIAOAI_COMMANDS 走「小爱同学」原生 ASR 文本截胡，不经过 KWS，
    无词表可言 → 不在本对账范围（deploy/config.py 内注释同口径）；
  - 「停止聆听」是 core 侧常驻隐私口令（kws 路由，STOP_LISTENING_KEYWORD），
    与指令表键同性质，一并要求入词表；
  - PERSONAS 按子串匹配 KWS 文本，不要求整词入表 → 不对账。
"""

import sys

from core.commands.router import STOP_LISTENING_KEYWORD
from core.utils.logger import logger


def missing_kws_keywords(direct_table, keywords) -> list[str]:
    """纯函数对账：免唤醒指令键（含「停止聆听」）未被词表覆盖的，按声明序去重。"""
    words = {str(w) for w in (keywords or []) if w}
    required = [str(k) for k in (direct_table or {}) if k]
    required.append(STOP_LISTENING_KEYWORD)

    missing: list[str] = []
    seen: set[str] = set()
    for key in required:
        if key not in words and key not in seen:
            seen.add(key)
            missing.append(key)
    return missing


def audit_kws_coverage() -> list[str]:
    """读活配置对账并告警。返回缺失键列表（供测试与调用方断言）。

    词表读 ConfigManager 活值（含面板 runtime overrides）；指令表读 config
    模块现值（reload 已先刷新模块属性再通知 listener，读到的是新表）。
    任何数据源不可用（config 模块未加载的测试环境等）降级跳过不报错——
    对账缺席绝不能变成崩溃，但必须有痕。
    """
    try:
        from core.utils.config import ConfigManager

        keywords = ConfigManager.instance().get_app_config("wakeup.keywords", [])
    except Exception as e:  # noqa: BLE001 - 词源缺失降级为不对账
        logger.warning(f"[commands] kws coverage audit skipped (keywords unavailable): {e}")
        return []

    module = sys.modules.get("config")
    direct = getattr(module, "DIRECT_COMMANDS", None) if module else None
    if not isinstance(direct, dict):
        logger.debug("[commands] kws coverage audit skipped (command table unavailable)")
        return []

    missing = missing_kws_keywords(direct, keywords)
    if missing:
        logger.warning(
            f"[commands] KWS 词表缺免唤醒指令键 {len(missing)} 个"
            f"（说了不触发，请补进 wakeup.keywords）: {missing}"
        )
    return missing
