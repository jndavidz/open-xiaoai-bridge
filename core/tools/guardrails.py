"""写操作护栏框架（P3 票 03）：A–D 四分级 + 渐进授权 + 二次确认。

纲领（《功能拓展展望》§4.4，2026-10-25 裁决）：

    read 只读，无确认
    A    状态查询 / 开关灯 / **家电控制（含红外）**——复述即执行（不等应答）
    B    媒体 / 窗帘 / 温度——白名单 + 数值范围，且音箱复述 + 用户应答
    C    **危险/非常规**：打印、摄像头快照、外发……——显式二次确认（不可逆动作强制）
    D    门锁 / 车库 / 报警 / 水阀 / 支付——永不交给 LLM（注册即过滤）

    2026-10-30 裁决：家电控制属常规操作，**不需要确认**（红外从 C 调到 A）；
    只有危险/非常规操作才保留 C 级二次确认。

渐进授权顺序「只读 → 草稿 → 审批后执行」；B 级以上（B/C）必须经
`ConfirmationManager` 的显式应答；D 级在 `get_all_tools`（模型不可见）
与 `execute_tool`（调用即拒绝留证）两道都被拦。

注入防线：LLM / 外部生成的文本进复述文案前必须过 `sanitize_for_speech`
（白名单字符集，剥引号/反斜杠/反引号/$/花括号与控制字符）——复述文案
会进 TTS 播报与日志，不得携带可执行语义。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from core.utils.logger import logger

LOG_MODULE = "Guardrails"

RISK_LEVELS = ("read", "a", "b", "c", "d")

CONFIRM_WORDS = ("确认", "执行", "是的", "对", "好的", "好", "是", "可以", "yes", "ok")
# 裸「不」不做否认词（「不错」是肯定义，歧义太大）；用双字组合。
DENY_WORDS = ("不要", "不用", "不行", "别", "取消", "停", "算了", "否认", "no", "cancel")

# 复述文案白名单：CJK/字母/数字/常用标点/空白。注意半角分号(;)、引号、
# 反斜杠、反引号、$、花括号**不入**白名单（shell 可执行语义）；全角标点
# （；等）对 shell 无意义，保留。
_ALLOWED = re.compile(r"[\u4e00-\u9fff\u3000-\u303fA-Za-z0-9 ，。、：；？！,.:?!\-_/ %+'()（）]")


def sanitize_for_speech(text: str) -> str:
    """复述文案注入防线：白名单外的字符替换为 ×（长度保持，便于察觉）。"""
    cleaned = "".join(
        " " if ch in "\n\r\t" else ch for ch in str(text or "")
    )
    return "".join(ch if _ALLOWED.match(ch) else "×" for ch in cleaned)


def match_confirmation(utterance: str) -> str | None:
    """用户应答分类："confirm" / "deny" / None（无关，不算应答）。

    否认词先于确认词扫描（保守：歧义句如「不要执行」按拒绝处理，
    宁可让用户再说一次，不可误执行）。
    """
    text = str(utterance or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in DENY_WORDS):
        return "deny"
    if any(word in text for word in CONFIRM_WORDS):
        return "confirm"
    return None


@dataclass
class PendingAction:
    action_id: str
    tool: str
    args: dict
    summary: str  # 已 sanitize 的复述文案
    created_at: float
    expires_at: float
    confirmed: bool = False
    resolved: str | None = None  # "executed" / "denied" / "expired" / "replaced"


class ConfirmationManager:
    """C/B 级显式二次确认状态机（纯内存；单设备串行，同工具顶替旧动作）。"""

    def __init__(self, ttl_seconds: float = 30.0):
        self.ttl = float(ttl_seconds)
        self._pending: dict[str, PendingAction] = {}

    def request(self, action_id: str, tool: str, args: dict, summary: str) -> PendingAction:
        """登记待确认动作；同 id 旧动作标记 replaced。"""
        now = time.monotonic()
        old = self._pending.get(action_id)
        if old is not None and old.resolved is None:
            old.resolved = "replaced"
        action = PendingAction(
            action_id=action_id,
            tool=tool,
            args=dict(args or {}),
            summary=sanitize_for_speech(summary),
            created_at=now,
            expires_at=now + self.ttl,
        )
        self._pending[action_id] = action
        return action

    def _live(self, action_id: str) -> PendingAction | None:
        action = self._pending.get(action_id)
        if action is None or action.resolved is not None:
            return None
        if time.monotonic() >= action.expires_at:
            action.resolved = "expired"
            logger.info(
                f"action={action_id} tool={action.tool} expired unconfirmed",
                module=LOG_MODULE,
            )
            return None
        return action

    def confirm(self, action_id: str) -> tuple[bool, str]:
        """用户应答确认：动作置 confirmed（执行由调用方完成）。"""
        action = self._live(action_id)
        if action is None:
            return False, "没有等待确认的操作（可能已超时或已处理）"
        action.confirmed = True
        return True, action.summary

    def deny(self, action_id: str) -> tuple[bool, str]:
        """用户应答否认：动作放弃。"""
        action = self._live(action_id)
        if action is None:
            return False, "没有等待确认的操作（可能已超时或已处理）"
        action.resolved = "denied"
        return True, "已取消，不执行该操作"

    def finish(self, action_id: str, outcome: str) -> None:
        """执行完成后收尾（resolved=executed / failed）。"""
        action = self._pending.get(action_id)
        if action is not None and action.resolved is None:
            action.resolved = outcome

    def expire_all(self) -> list[str]:
        """惰性清理全部已过期未决动作；返回被放弃的 action_id。"""
        expired: list[str] = []
        now = time.monotonic()
        for action_id, action in list(self._pending.items()):
            if action.resolved is None and now >= action.expires_at:
                action.resolved = "expired"
                expired.append(action_id)
        return expired

    def pending_action(self, action_id: str) -> PendingAction | None:
        return self._live(action_id)


def module_risk(mod) -> str | None:
    """取工具模块的分级声明；未声明或非法返回 None（= 拒绝注册）。"""
    risk = str(getattr(mod, "RISK", "") or "").strip().lower()
    return risk if risk in RISK_LEVELS else None


def describe_action(tool: str, args: dict) -> str:
    """生成复述文案（注入防线：参数值过 sanitize）。"""
    parts = [f"{key} {sanitize_for_speech(str(value))}" for key, value in (args or {}).items()]
    detail = "，".join(parts)
    return f"即将执行 {sanitize_for_speech(tool)}" + (f"：{detail}" if detail else "")


async def execute_guarded(
    mod,
    args: dict,
    *,
    action_id: str | None = None,
    confirmation: ConfirmationManager | None = None,
    announcement=None,
    confirm_utterance: str | None = None,
) -> str:
    """按分级执行写工具（全路径纯内存可测）。

    - read / a：复述（announcement 回调，A 级一次确认=播报复述即执行）后直接执行；
    - b / c：无 confirm_utterance ⇒ 登记 pending 并返回复述请求（不执行）；
      有 confirm_utterance ⇒ 按应答分支执行 / 放弃；
    - d：直接拒绝并留证（模型本就看不到 D 级工具，此处是纵深防御）；
    - B 级白名单/数值范围校验经模块可选 `validate(args) -> str | None`
      （返回拒绝原因即拒绝执行）。
    announcement: async callable(text) —— 生产接 speaker.announce（双闸门）。
    """
    risk = module_risk(mod)
    name = getattr(mod, "NAME", "<unnamed>")
    if risk is None:
        logger.error(f"tool {name} has no valid RISK declaration; refusing", module=LOG_MODULE)
        return "tool_error: 工具未声明风险分级，拒绝执行"
    if risk == "d":
        logger.warning(f"tool {name} is risk-D (never allowed); call refused", module=LOG_MODULE)
        return "该操作属于永不开放的危险类别（D 级），已拒绝并记录"

    if risk == "b":
        validator = getattr(mod, "validate", None)
        if callable(validator):
            reason = validator(args or {})
            if reason:
                return f"已拒绝：{sanitize_for_speech(str(reason))}"

    summary = describe_action(name, args or {})
    if risk in ("read", "a"):
        if risk == "a" and announcement is not None:
            await announcement(summary)
        return await mod.run(args or {})

    # B / C：显式二次确认
    manager = confirmation or ConfirmationManager()
    manager.expire_all()
    if action_id is None:
        action_id = f"{name}:latest"
    if not confirm_utterance:
        manager.request(action_id, name, args or {}, summary)
        logger.info(
            f"tool {name} awaiting explicit confirmation (action={action_id})",
            module=LOG_MODULE,
        )
        return f"需要确认：{summary}。请说『确认』执行，或说『取消』放弃。"

    verdict = match_confirmation(confirm_utterance)
    if verdict == "deny":
        manager.deny(action_id)
        return "已取消，不执行该操作"
    if verdict == "confirm":
        action = manager.pending_action(action_id)
        if action is None:
            return "确认已过期或没有等待确认的操作，请重新发起"
        ok, _ = manager.confirm(action_id)
        if not ok:
            return "确认失败，请重新发起"
        try:
            result = await mod.run(dict(action.args))
        except Exception as exc:  # noqa: BLE001 - 工具异常收敛为错误串
            manager.finish(action_id, "failed")
            return f"tool_error: {type(exc).__name__}: {exc}"
        manager.finish(action_id, "executed")
        return result
    return "没有听清确认或取消，操作未执行；请重新发起"


async def handle_confirmation_utterance(utterance: str) -> str | None:
    """对话层应答拦截（P3 票 04 接线）：ASR 定稿处先于 LLM 调用。

    有等待确认的写操作且用户话语是确认/否认 ⇒ 执行或放弃，返回播报
    文本（对话层 announce 后跳过本轮 LLM）；无 pending 或话语无关 ⇒
    返回 None（本轮照常走 LLM）。
    """
    from core.tools import registry as tools_registry

    manager = tools_registry.confirmation_manager()
    manager.expire_all()
    # 单设备串行：同名 id 顶替 ⇒ 至多一个活动 pending（:latest id 约定）
    action = manager.pending_action(_LATEST_ID(manager))
    if action is None:
        return None
    verdict = match_confirmation(utterance)
    if verdict is None:
        return None
    if verdict == "deny":
        manager.deny(action.action_id)
        return "好的，已取消该操作。"
    ok, summary = manager.confirm(action.action_id)
    if not ok:
        return None
    mod = tools_registry._REGISTRY.get(action.tool)
    if mod is None:
        manager.finish(action.action_id, "failed")
        return "确认的操作已不可用，未执行。"
    try:
        result = await mod.run(dict(action.args))
    except Exception as exc:  # noqa: BLE001
        manager.finish(action.action_id, "failed")
        logger.error(
            f"confirmed action {action.tool} failed: {exc}", module=LOG_MODULE
        )
        return f"执行失败：{exc}"
    manager.finish(action.action_id, "executed")
    return f"已确认并执行：{sanitize_for_speech(action.summary)}。结果：{result}"


def _LATEST_ID(manager: ConfirmationManager) -> str:
    """约定 id 后缀（execute_tool 用 `<tool>:latest`）；取任一活动 pending。"""
    for action_id, action in manager._pending.items():
        if action.resolved is None and action_id.endswith(":latest"):
            return action_id
    return ""
