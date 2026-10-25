"""P3 票 01 只读三件套契约测试——纯内存（FakeDevice 边界）。

覆盖：

  1. A-4 健康面：nightmode_current / firmware_info 能力表登记契约与解析；
     micocfg setter 组入脚本禁区闸门、白名单 getter 命令放行；
  2. A-3 音量护栏：失败时按夜间模式非对称调容语义归因（日常调小点名、
     夜间/调大走普通失败日志、成功路径不查 nightmode）；
  3. A-5 设备状态工具：device_status 经注册表可调可答（只读标注、
     凭证零接触——数据源只有能力层只读白名单行）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fake_device import FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import device as device_pkg, ref  # noqa: E402
from core.device import registry  # noqa: E402
from core.device.registry import ForbiddenCapabilityError  # noqa: E402
from core.utils.logger import logger as app_logger  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(coro):
    return asyncio.run(coro)


def _context(status: int = 1, volume: int = 39) -> str:
    return json.dumps(
        {
            "code": 0,
            "info": json.dumps(
                {"status": status, "volume": volume, "player_mode": 1}
            ),
        },
        ensure_ascii=False,
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        ref.GLOBAL_STATES.clear()
        device_pkg.reset_device()
        self.device = FakeDevice()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        self.caps = device_pkg.get_device()
        self.speaker = speaker_module.SpeakerManager()
        self.addCleanup(device_pkg.reset_device)
        self.addCleanup(ref.GLOBAL_STATES.clear)


class NightmodeCapabilityTest(_Base):
    def test_registered_with_read_contract(self) -> None:
        spec = registry.resolve_capability("nightmode_current")
        self.assertEqual(registry.CapabilityGroup.READ, spec.group)
        self.assertEqual(registry.SuccessCriterion.JSON_PARSED, spec.success)
        self.assertTrue(spec.idempotent)

    def test_reads_volume_field(self) -> None:
        self.device.on(
            contains="nightmode current",
            stdout=json.dumps({"total": "night", "light": "normal", "volume": "night"}),
        )
        res = _run(self.caps.nightmode_current())
        self.assertTrue(res.ok, res.error)
        self.assertEqual({"volume": "night"}, res.readback)

    def test_unreadable_response_is_failure(self) -> None:
        self.device.on(contains="nightmode current", stdout="garbage")
        res = _run(self.caps.nightmode_current())
        self.assertFalse(res.ok)
        self.assertIsNone((res.readback or {}).get("volume"))


class FirmwareInfoCapabilityTest(_Base):
    def test_registered_with_read_contract(self) -> None:
        spec = registry.resolve_capability("firmware_info")
        self.assertEqual(registry.CapabilityGroup.READ, spec.group)
        self.assertEqual(registry.SuccessCriterion.EXIT_CODE, spec.success)

    def test_parses_whitelist_key_values(self) -> None:
        self.device.on(
            contains="micocfg_version",
            stdout=(
                "version=1.94.13 build_time=2025-09-17 board=lx06_nxp8987 "
                "board_id=10 wifi_chip=nxp8987 work_mode=normal\n"
            ),
        )
        res = _run(self.caps.firmware_info())
        self.assertTrue(res.ok, res.error)
        self.assertEqual("1.94.13", res.readback.get("version"))
        self.assertEqual("nxp8987", res.readback.get("wifi_chip"))
        self.assertNotIn("sn", res.readback)
        self.assertNotIn("mac", res.readback)

    def test_command_uses_only_whitelist_getters(self) -> None:
        """命令形状只含白名单 getter——扩展到敏感字段此处即红。"""
        from core.device.client import FIRMWARE_INFO

        for forbidden in ("micocfg_sn", "micocfg_mac", "micocfg_ssid", "micocfg_set"):
            self.assertNotIn(forbidden, FIRMWARE_INFO)
        for required in (
            "micocfg_version",
            "micocfg_build_time",
            "micocfg_board_name",
            "micocfg_wifi_chip",
            "micocfg_work_mode",
        ):
            self.assertIn(required, FIRMWARE_INFO)

    def test_system_memory_now_carries_uptime(self) -> None:
        self.device.on(
            contains="system info",
            stdout=json.dumps(
                {"uptime": 14208, "memory": {"total": 118000, "free": 5000000}}
            ),
        )
        res = _run(self.caps.system_memory())
        self.assertTrue(res.ok)
        self.assertEqual(14208, res.readback.get("uptime"))


class MicocfgForbiddenGateTest(_Base):
    def test_setter_scripts_are_blocked(self) -> None:
        for script in (
            "micocfg_set language zh",
            "micocfg_sys_status_set 1",
            "micocfg_bt_enable_set true",
            "micocfg_miio_register ssid psk",
            "micocfg_wifi_scan",
            "micocfg_light_show 3",
        ):
            with self.assertRaises(ForbiddenCapabilityError, msg=script):
                registry.ensure_script_allowed(script)

    def test_whitelist_getters_still_pass(self) -> None:
        from core.device.client import FIRMWARE_INFO, DEVICE_INFO

        registry.ensure_script_allowed(FIRMWARE_INFO)
        registry.ensure_script_allowed(DEVICE_INFO)  # 含 micocfg_sn：读路径放行（SN 不进健康面/工具）


class VolumeNightmodeGuardrailTest(_Base):
    def test_daytime_downgrade_failure_is_attributed_to_nightmode(self) -> None:
        # 设前音量 39，调到 20 失败（回读仍 39 = 被设备纠正）⇒ 日常时段：
        # 点名夜间模式非对称调容语义（A-3：不再误判为未知 bug）
        self.device.on(contains="nightmode current",
                       stdout=json.dumps({"volume": "normal", "total": "normal"}))
        self.device.on(contains="player_get_context", stdout=_context(volume=39))
        self.device.on(
            contains="player_set_continuous_volume", stdout=_context(volume=39)
        )
        with mock.patch.object(app_logger, "warning") as warning:
            ok = _run(self.speaker.set_volume(20))
        self.assertFalse(ok)
        messages = [str(c.args[0]) for c in warning.call_args_list]
        self.assertTrue(
            any("只允许调大" in m and "非 bug" in m for m in messages), messages
        )

    def test_nighttime_failure_keeps_plain_log(self) -> None:
        self.device.on(contains="player_get_context", stdout=_context(volume=39))
        self.device.on(
            contains="player_set_continuous_volume", stdout=_context(volume=39)
        )
        self.device.on(
            contains="nightmode current",
            stdout=json.dumps({"volume": "night", "total": "night"}),
        )
        with mock.patch.object(app_logger, "warning") as warning:
            ok = _run(self.speaker.set_volume(20))
        self.assertFalse(ok)
        messages = [str(c.args[0]) for c in warning.call_args_list]
        self.assertFalse(any("只允许调大" in m for m in messages), messages)

    def test_success_path_does_not_query_nightmode(self) -> None:
        # 序列：设前 get=39，set 回读=20（写入成功）
        from fake_device import CommandResult as FakeResult

        self.device.on_sequence(
            "player_get_context",
            [FakeResult(_context(volume=39), "", 0), FakeResult(_context(volume=20), "", 0)],
        )
        self.device.on(
            contains="player_set_continuous_volume",
            stdout=_context(volume=20),
        )
        ok = _run(self.speaker.set_volume(20))
        self.assertTrue(ok)
        self.device.assert_not_called("nightmode current")


class DeviceStatusToolTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        ref.set_speaker(self.speaker)

    def _register_fakes(self) -> None:
        self.device.on(contains="player_get_context", stdout=_context(status=1, volume=39))
        self.device.on(contains="mphelper mute_stat", stdout="1")
        self.device.on(contains="/tmp/mipns/mute", stdout="on")
        self.device.on(
            contains="system info",
            stdout=json.dumps(
                {"uptime": 100, "memory": {"total": 118000000, "free": 5000000}}
            ),
        )
        self.device.on(
            contains="nightmode current",
            stdout=json.dumps({"volume": "normal", "total": "normal"}),
        )

    def test_tool_registered_as_readonly(self) -> None:
        from core.tools import registry as tools_registry

        names = [t["function"]["name"] for t in tools_registry.get_all_tools()]
        self.assertIn("device_status", names)
        spec = next(
            t for t in tools_registry.get_all_tools()
            if t["function"]["name"] == "device_status"
        )
        self.assertIn("只读", spec["function"]["description"])

    def test_tool_executes_via_registry(self) -> None:
        from core.tools import registry as tools_registry

        self._register_fakes()
        result = _run(tools_registry.execute_tool("device_status", "{}"))
        self.assertIn("播放中", result)
        self.assertIn("39", result)
        self.assertIn("夜间模式", result)
        # 凭证零接触：全程无 micocfg 敏感 getter / miot_rpc 调用
        for sensitive in ("micocfg_sn", "micocfg_mac", "miot_rpc"):
            self.device.assert_not_called(sensitive)

    def test_tool_reports_unavailable_speaker(self) -> None:
        from core.tools import device_status

        ref.GLOBAL_STATES.clear()
        result = _run(device_status.run({}))
        self.assertIn("不可用", result)


if __name__ == "__main__":
    unittest.main()
