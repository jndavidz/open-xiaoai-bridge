"""红外收编（A-7/P3 票 04）契约测试——纯内存（FakeDevice 边界）。

覆盖：

  1. 能力表 ir_send 登记契约 + client 校验面（字符集/偶数项/限长/正值——
     安全红线同款：非法字符、奇数项、超长、超总时长全拒绝；命令注入
     免疫由字符集白名单保证）；
  2. C 级工具：码值表查找（device/action → µs 序列，LLM 不编序列）、
     未配置动作拒绝、speaking 态不与播放并发、串行锁、护栏分级声明；
  3. 网络复验记录见票 Answer（真机实测，非本文件范畴）；
     端到端（语音 → 复述确认 → 发射 → 内核日志）留真机窗口。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
for _path in (TESTS_DIR, ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fake_device import FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import device as device_pkg, ref  # noqa: E402
from core.device import registry  # noqa: E402
from core.tools import guardrails  # noqa: E402
from core.tools import ir_send as ir_tool  # noqa: E402
from core.tools import registry as tools_registry  # noqa: E402
from core.utils import pipeline as pipeline_module  # noqa: E402
from core.utils.config import ConfigManager  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


NEC_LIKE = ",".join(["9000", "4500"] + ["560", "560", "1680", "560"] * 4)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        ref.GLOBAL_STATES.clear()
        device_pkg.reset_device()
        pipeline_module.reset_pipeline()
        self.device = FakeDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        ref.set_speaker(speaker_module.SpeakerManager())
        self.addCleanup(device_pkg.reset_device)
        self.addCleanup(pipeline_module.reset_pipeline)
        self.addCleanup(ref.GLOBAL_STATES.clear)


class IrSendCapabilityTest(_Base):
    def test_registered_with_carrier_notes(self) -> None:
        spec = registry.resolve_capability("ir_send")
        self.assertTrue(spec.blocking)
        self.assertTrue(spec.notes)
        self.assertTrue(any("关中断" in n for n in spec.notes))

    def test_validation_rejects_bad_patterns(self) -> None:
        caps = device_pkg.get_device()
        for bad in (
            "9000",                       # 奇数项
            "9000,4500,;rm",              # 非法字符
            "9000,-4500",                 # 负数
            "9000,0",                     # 零
            "",                           # 空
            ",".join(["100", "100"] * 300),  # 超长（600 > 512）
            ",".join(["100000", "100000"] * 2),  # 总时长超限
        ):
            with self.assertRaises(ValueError, msg=bad[:30]):
                _run(caps.ir_send(bad))

    def test_valid_pattern_writes_sysfs(self) -> None:
        self.device.on(contains="ir_tx_gpio", stdout="")
        caps = device_pkg.get_device()
        res = _run(caps.ir_send(NEC_LIKE))
        self.assertTrue(res.ok, res.error)
        script = self.device.matching_calls("ir_tx_gpio")[0]
        self.assertIn("echo " + NEC_LIKE + " > /sys/ir_tx_gpio/ir_data", script)


class IrSendToolTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self._codes = {"ac/on": NEC_LIKE, "fan/off": NEC_LIKE}
        self.config = ConfigManager.instance()
        patcher = mock.patch.object(
            self.config, "get_app_config", side_effect=self._get_config
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _get_config(self, key, default=None):
        if key == "ir.codes":
            return self._codes
        return default

    def test_risk_declaration_and_registration(self) -> None:
        # 2026-10-30 裁决：家电控制属常规操作，A 级（复述即执行、不等应答）
        self.assertEqual("a", guardrails.module_risk(ir_tool))
        self.assertIn("ir_send", tools_registry._REGISTRY)
        names = [t["function"]["name"] for t in tools_registry.get_all_tools()]
        self.assertIn("ir_send", names)  # C 级对模型可见（D 级才隐藏）

    def test_lookup_and_send(self) -> None:
        self.device.on(contains="ir_tx_gpio", stdout="")
        result = _run(ir_tool.run({"device": "ac", "action": "on"}))
        self.assertIn("已发射", result)
        self.device.assert_called("ir_tx_gpio")

    def test_unknown_code_refused(self) -> None:
        result = _run(ir_tool.run({"device": "tv", "action": "power"}))
        self.assertIn("没有配置", result)
        self.assertEqual(0, len(self.device.calls))

    def test_llm_supplied_pattern_is_ignored(self) -> None:
        """LLM 只传 device/action；即使幻觉出 pattern 参数也不被采用。"""
        self.device.on(contains="ir_tx_gpio", stdout="")
        result = _run(
            ir_tool.run({"device": "ac", "action": "on", "pattern": "1,2"})
        )
        self.assertIn("已发射", result)
        script = self.device.matching_calls("ir_tx_gpio")[0]
        self.assertIn(NEC_LIKE, script)  # 用的是码值表，不是幻觉参数

    def test_no_send_while_speaking(self) -> None:
        self.device.on(contains="ir_tx_gpio", stdout="")
        pipeline_module.begin_turn("local_asr")
        pipeline_module.set_state(pipeline_module.PipelineState.SPEAKING)
        result = _run(ir_tool.run({"device": "ac", "action": "on"}))
        self.assertIn("正在播放", result)
        self.assertEqual(0, len(self.device.calls))

    def test_a_level_guarded_flow(self) -> None:
        """经 execute_guarded（A 级）：复述后**直接执行**，不登记 pending。"""
        self.device.on(contains="ir_tx_gpio", stdout="")
        manager = guardrails.ConfirmationManager(ttl_seconds=60)
        recited: list[str] = []

        async def _announce(text: str) -> None:
            recited.append(text)

        result = _run(
            guardrails.execute_guarded(
                ir_tool,
                {"device": "ac", "action": "on"},
                action_id="ir:1",
                confirmation=manager,
                announcement=_announce,
            )
        )
        self.assertNotIn("需要确认", result)
        self.assertIn("已发射", result)
        self.assertTrue(recited, "A 级必须先复述再执行")
        self.device.assert_called("ir_tx_gpio")
        self.assertEqual({}, manager._pending, "A 级不得登记待确认动作")


if __name__ == "__main__":
    unittest.main()
