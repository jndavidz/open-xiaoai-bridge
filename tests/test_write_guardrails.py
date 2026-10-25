"""写操作护栏框架（P3 票 03）契约测试——纯内存，假写工具全路径。

覆盖（票面四验收）：

  1. 分级元数据 + 注册表校验：未声明 RISK 拒注册；现有只读工具已声明；
  2. C 级二次确认流程：复述文案（注入转义）→ 用户应答三分支（确认执行/
     否认放弃/超时放弃）；
  3. 假写工具全路径：A 一次确认（复述即执行）/ B 范围拒绝 / C 三分支 /
     D 拒绝留证；
  4. D 级工具对模型不可见（get_all_tools 过滤）且 execute_tool 拒绝。
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.tools import guardrails  # noqa: E402
from core.tools import registry as tools_registry  # noqa: E402
from core.tools.guardrails import (  # noqa: E402
    ConfirmationManager,
    execute_guarded,
    match_confirmation,
    module_risk,
    sanitize_for_speech,
)
from core.utils.logger import logger as app_logger  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _fake_tool(name: str, risk: str, *, validate=None) -> types.ModuleType:
    """造一个假工具模块（run 记账；validate 可选 B 级范围校验）。"""
    mod = types.ModuleType(f"fake_{name}")
    mod.NAME = name
    mod.RISK = risk
    mod.calls: list[dict] = []
    mod.announced: list[str] = []

    async def _run_tool(args):
        mod.calls.append(dict(args))
        return f"{name} ok"

    mod.run = _run_tool
    if validate is not None:
        mod.validate = validate
    return mod


# ---- 注入防线与应答分类 ----


class SanitizeTest(unittest.TestCase):
    def test_dangerous_characters_replaced(self):
        cleaned = sanitize_for_speech('关灯"; rm -rf / # $(echo hi) `x` {a}')
        for dangerous in ('"', ";", "$", "`", "{", "}"):
            self.assertNotIn(dangerous, cleaned)
        self.assertIn("关灯", cleaned)

    def test_normal_chinese_kept(self):
        text = "把客厅空调设为 26 度，风速自动"
        self.assertEqual(text, sanitize_for_speech(text))

    def test_control_characters_become_spaces(self):
        self.assertNotIn("\n", sanitize_for_speech("a\nb\r\tc"))


class MatchConfirmationTest(unittest.TestCase):
    def test_confirm_deny_unrelated(self):
        self.assertEqual("confirm", match_confirmation("确认"))
        self.assertEqual("confirm", match_confirmation("好的，执行吧"))
        self.assertEqual("deny", match_confirmation("算了，取消"))
        self.assertEqual("deny", match_confirmation("别"))
        self.assertIsNone(match_confirmation("今天天气怎么样"))
        self.assertIsNone(match_confirmation(""))
        # 裸「不」歧义（「不错」是肯定义）：不算否认，也不算确认
        self.assertIsNone(match_confirmation("不错"))
        self.assertEqual("deny", match_confirmation("不要执行"))


# ---- 二次确认状态机 ----


class ConfirmationManagerTest(unittest.TestCase):
    def test_request_confirm_deny_expire(self):
        mgr = ConfirmationManager(ttl_seconds=0.05)
        action = mgr.request("ir:latest", "ir_send", {"device": "ac"}, "打开空调")
        self.assertFalse(action.confirmed)

        ok, _ = mgr.confirm("ir:latest")
        self.assertTrue(ok)
        mgr.finish("ir:latest", "executed")
        # 已收尾后再确认：拒绝
        ok, msg = mgr.confirm("ir:latest")
        self.assertFalse(ok)

    def test_deny_resolves_action(self):
        mgr = ConfirmationManager()
        mgr.request("x:1", "x", {}, "动作")
        ok, _ = mgr.deny("x:1")
        self.assertTrue(ok)
        self.assertFalse(mgr.pending_action("x:1") is not None)

    def test_expiry_drops_action(self):
        mgr = ConfirmationManager(ttl_seconds=0.01)
        mgr.request("x:1", "x", {}, "动作")
        import time

        time.sleep(0.02)
        expired = mgr.expire_all()
        self.assertEqual(["x:1"], expired)
        self.assertFalse(mgr.confirm("x:1")[0])

    def test_same_id_replaces_pending(self):
        mgr = ConfirmationManager()
        mgr.request("ir:latest", "ir_send", {"device": "fan"}, "开风扇")
        mgr.request("ir:latest", "ir_send", {"device": "ac"}, "开空调")
        # 单设备串行：同 id 后发顶替先发——先发动作已不是可确认的 pending
        current = mgr.pending_action("ir:latest")
        self.assertIsNotNone(current)
        self.assertEqual("开空调", current.summary)
        self.assertEqual({"device": "ac"}, current.args)


# ---- 假写工具全路径 ----


class ExecuteGuardedTest(unittest.TestCase):
    def setUp(self):
        self.manager = ConfirmationManager(ttl_seconds=60)
        self.announcements: list[str] = []

        async def _announce(text):
            self.announcements.append(text)

        self.announce = _announce

    def test_a_level_announces_once_then_executes(self):
        tool = _fake_tool("light_set", "a")
        result = _run(
            execute_guarded(tool, {"room": "客厅"}, announcement=self.announce)
        )
        self.assertEqual("light_set ok", result)
        self.assertEqual(1, len(tool.calls))
        self.assertEqual(1, len(self.announcements))
        self.assertIn("light_set", self.announcements[0])
        self.assertIn("客厅", self.announcements[0])

    def test_b_level_range_rejected(self):
        def _validate(args):
            temp = args.get("temperature")
            if not isinstance(temp, (int, float)) or not 16 <= temp <= 30:
                return "温度只允许 16-30 度"
            return None

        tool = _fake_tool("ac_set", "b", validate=_validate)
        result = _run(
            execute_guarded(
                tool, {"temperature": 55}, confirmation=self.manager
            )
        )
        self.assertIn("已拒绝", result)
        self.assertEqual([], tool.calls)

    def test_c_level_three_branches(self):
        tool = _fake_tool("ir_send", "c")

        # 分支 1：首次调用 → 不执行，返回复述请求（注入转义）
        result = _run(
            execute_guarded(
                tool,
                {"device": '空调"; rm -rf /'},
                action_id="ir:1",
                confirmation=self.manager,
            )
        )
        self.assertEqual([], tool.calls)
        self.assertIn("需要确认", result)
        # 注入防线语义：可执行语义字符（引号/分号/$/反引号）被剥成 ×
        summary = self.manager.pending_action("ir:1").summary
        for dangerous in (";", "\"", "$", "`"):
            self.assertNotIn(dangerous, summary)

        # 分支 2：否认 → 放弃
        result = _run(
            execute_guarded(
                tool,
                {"device": "空调"},
                action_id="ir:2",
                confirmation=self.manager,
                confirm_utterance="算了，取消",
            )
        )
        self.assertIn("已取消", result)
        self.assertEqual([], tool.calls)

        # 分支 3a：确认（无 pending）→ 过期语义
        result = _run(
            execute_guarded(
                tool,
                {"device": "空调"},
                action_id="ir:none",
                confirmation=self.manager,
                confirm_utterance="确认",
            )
        )
        self.assertIn("过期", result)
        self.assertEqual([], tool.calls)

        # 分支 3b：先登记再确认 → 执行
        _run(
            execute_guarded(
                tool, {"device": "空调"}, action_id="ir:3", confirmation=self.manager
            )
        )
        result = _run(
            execute_guarded(
                tool,
                {"device": "空调"},
                action_id="ir:3",
                confirmation=self.manager,
                confirm_utterance="确认",
            )
        )
        self.assertEqual("ir_send ok", result)
        self.assertEqual(1, len(tool.calls))

    def test_d_level_refused_with_audit_log(self):
        tool = _fake_tool("door_unlock", "d")
        with mock.patch.object(app_logger, "warning") as warning:
            result = _run(execute_guarded(tool, {}))
        self.assertIn("D 级", result)
        self.assertEqual([], tool.calls)
        self.assertTrue(
            any("risk-D" in str(c.args[0]) for c in warning.call_args_list)
        )

    def test_missing_risk_declaration_refused(self):
        tool = _fake_tool("ghost", "read")
        tool.RISK = "super"
        result = _run(execute_guarded(tool, {}))
        self.assertIn("拒绝执行", result)

    def test_unrelated_utterance_does_not_execute(self):
        tool = _fake_tool("ir_send", "c")
        _run(
            execute_guarded(
                tool, {"device": "风扇"}, action_id="ir:9", confirmation=self.manager
            )
        )
        result = _run(
            execute_guarded(
                tool,
                {"device": "风扇"},
                action_id="ir:9",
                confirmation=self.manager,
                confirm_utterance="今天天气不错",
            )
        )
        self.assertIn("没有听清", result)
        self.assertEqual([], tool.calls)


# ---- 注册表校验与 D 级不可见 ----


class RegistryGuardrailTest(unittest.TestCase):
    def test_existing_readonly_tools_declare_risk(self):
        import core.tools.hass as hass_mod
        import core.tools.weather as weather_mod
        import core.tools.music as music_mod
        import core.tools.device_status as status_mod

        for mod in (hass_mod, weather_mod, music_mod, status_mod):
            self.assertEqual("read", module_risk(mod), mod.__name__)
            self.assertIn(mod.NAME, tools_registry._REGISTRY)

    def test_d_level_tool_hidden_from_model_and_refused(self):
        tool = _fake_tool("garage_door", "d")
        tool.TOOL_SPEC = {
            "type": "function",
            "function": {"name": "garage_door", "description": "x", "parameters": {}},
        }
        saved = dict(tools_registry._REGISTRY)
        try:
            tools_registry._REGISTRY["garage_door"] = tool
            names = [t["function"]["name"] for t in tools_registry.get_all_tools()]
            self.assertNotIn("garage_door", names)
            result = _run(tools_registry.execute_tool("garage_door", "{}"))
            self.assertIn("D 级", result)
        finally:
            tools_registry._REGISTRY.clear()
            tools_registry._REGISTRY.update(saved)


# ---- P3-04 对话层接线：execute_tool 走 guarded + handle_confirmation_utterance ----


class DialogueWiringTest(unittest.TestCase):
    def setUp(self):
        from core.tools import registry as tr

        self.tr = tr
        self._saved = dict(tr._REGISTRY)
        self._saved_cm = tr._CONFIRMATIONS
        tr._CONFIRMATIONS = guardrails.ConfirmationManager(ttl_seconds=60)
        self.tool = _fake_tool("ir_send", "c")
        tr._REGISTRY["ir_send"] = self.tool
        self.addCleanup(self._restore)

    def _restore(self):
        self.tr._REGISTRY.clear()
        self.tr._REGISTRY.update(self._saved)
        self.tr._CONFIRMATIONS = self._saved_cm

    def test_execute_tool_c_level_first_call_returns_recap(self):
        result = _run(self.tr.execute_tool("ir_send", '{"device": "ac"}'))
        self.assertIn("需要确认", result)
        self.assertEqual([], self.tool.calls)  # 未执行
        action = self.tr.confirmation_manager().pending_action("ir_send:latest")
        self.assertIsNotNone(action)
        self.assertEqual({"device": "ac"}, action.args)

    def test_handle_confirmation_utterance_confirm_executes(self):
        _run(self.tr.execute_tool("ir_send", '{"device": "ac"}'))
        reply = _run(guardrails.handle_confirmation_utterance("确认"))
        self.assertIsNotNone(reply)
        self.assertIn("已确认并执行", reply)
        self.assertEqual([{"device": "ac"}], self.tool.calls)
        # 收尾后再答：不再拦截（走 LLM）
        self.assertIsNone(_run(guardrails.handle_confirmation_utterance("确认")))

    def test_handle_confirmation_utterance_deny(self):
        _run(self.tr.execute_tool("ir_send", '{"device": "ac"}'))
        reply = _run(guardrails.handle_confirmation_utterance("算了，取消"))
        self.assertIn("已取消", reply)
        self.assertEqual([], self.tool.calls)

    def test_unrelated_utterance_returns_none(self):
        _run(self.tr.execute_tool("ir_send", '{"device": "ac"}'))
        self.assertIsNone(_run(guardrails.handle_confirmation_utterance("今天天气怎么样")))
        self.assertIsNone(_run(guardrails.handle_confirmation_utterance("")))

    def test_no_pending_returns_none(self):
        self.assertIsNone(_run(guardrails.handle_confirmation_utterance("确认")))


if __name__ == "__main__":
    unittest.main()
