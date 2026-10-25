"""词表 ⊇ 指令表键 启动校验（P2 ticket 02）的契约测试。

被测对象 = core/commands/audit.py（纯函数对账 + 活配置读数的 audit_kws_coverage）
与 core/app.py 的挂点（开机 + 热重载 listener）。全部内存替身，不连设备。

覆盖（对应 ticket 02 验收项 2）：
  1. 缺键构造 → 告警点名（WARN 文案含全部缺失键名，按声明序）；
  2. 齐全 → 静默（无 WARN）；
  3. 「停止聆听」与指令键同性质，缺它同样点名；
  4. 数据源不可用（config 模块未加载 / ConfigManager 异常）→ 降级跳过不崩溃；
  5. deploy/config.py 真实数据面守门：生产两张配置当前零缺失（漂移即红）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# core.app import 期需要原生扩展占位（同 test_backend_registry 的口径）
sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

from core.commands.audit import audit_kws_coverage, missing_kws_keywords  # noqa: E402
from core.commands.router import STOP_LISTENING_KEYWORD  # noqa: E402


def _load_deploy_config(module_name: str = "deploy_config_for_kws_audit"):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "deploy" / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MissingKwsKeywordsTest(unittest.TestCase):
    def test_missing_keys_named_in_declaration_order(self):
        table = {"音乐模式": [], "打开空调": [], "下一首歌曲": [], "暂停音乐": []}
        keywords = ["音乐模式", "暂停音乐"]
        self.assertEqual(
            missing_kws_keywords(table, keywords),
            ["打开空调", "下一首歌曲", STOP_LISTENING_KEYWORD],
        )

    def test_complete_is_silent(self):
        table = {"音乐模式": [], "下一首歌曲": []}
        self.assertEqual(
            missing_kws_keywords(
                table, ["音乐模式", "下一首歌曲", "你好贾维斯", STOP_LISTENING_KEYWORD]
            ),
            [],
        )

    def test_stop_listening_is_required_too(self):
        self.assertEqual(missing_kws_keywords({}, []), [STOP_LISTENING_KEYWORD])
        self.assertEqual(
            missing_kws_keywords({"音乐模式": []}, ["音乐模式", STOP_LISTENING_KEYWORD]), []
        )

    def test_empty_or_none_inputs(self):
        self.assertEqual(missing_kws_keywords(None, None), [STOP_LISTENING_KEYWORD])
        self.assertEqual(missing_kws_keywords({}, []), [STOP_LISTENING_KEYWORD])

    def test_all_missing_reported_once_each(self):
        table = {"音乐模式": [], "下一首歌曲": []}
        self.assertEqual(
            missing_kws_keywords(table, []), ["音乐模式", "下一首歌曲", STOP_LISTENING_KEYWORD]
        )


class _StubConfigManager:
    def __init__(self, keywords):
        self._keywords = keywords

    def get_app_config(self, path, default=None):
        assert path == "wakeup.keywords"
        return self._keywords


class AuditKwsCoverageTest(unittest.TestCase):
    """audit_kws_coverage：sys.modules["config"] + ConfigManager 双词源的活读数。"""

    def _run_with(self, direct_table, keywords):
        fake_config = types.ModuleType("config_for_kws_audit")
        fake_config.DIRECT_COMMANDS = direct_table
        with mock.patch.dict(sys.modules, {"config": fake_config}):
            with mock.patch(
                "core.utils.config.ConfigManager.instance",
                return_value=_StubConfigManager(keywords),
            ):
                return audit_kws_coverage()

    def test_missing_keys_warn_with_all_names(self):
        with self.assertLogs("xiaozhi", level="WARNING") as captured:
            missing = self._run_with(
                {"音乐模式": [], "打开空调": [], "暂停音乐": []},
                ["音乐模式", "暂停音乐"],
            )
        self.assertEqual(missing, ["打开空调", STOP_LISTENING_KEYWORD])
        warn_texts = [line for line in captured.output if "WARNING" in line]
        self.assertTrue(any("打开空调" in line for line in warn_texts))
        self.assertTrue(any(STOP_LISTENING_KEYWORD in line for line in warn_texts))
        self.assertTrue(any("wakeup.keywords" in line for line in warn_texts))

    def test_complete_no_warning(self):
        with self.assertNoLogs("xiaozhi", level="WARNING"):
            missing = self._run_with({"音乐模式": []}, ["音乐模式", STOP_LISTENING_KEYWORD])
        self.assertEqual(missing, [])

    def test_config_module_absent_degrades_silently(self):
        # 部分测试环境无 config 模块：跳过对账，不告警不崩溃
        saved = sys.modules.pop("config", None)
        try:
            with mock.patch(
                "core.utils.config.ConfigManager.instance",
                return_value=_StubConfigManager([]),
            ):
                self.assertEqual(audit_kws_coverage(), [])
        finally:
            if saved is not None:
                sys.modules["config"] = saved

    def test_config_manager_failure_degrades_with_trace(self):
        fake_config = types.ModuleType("config_for_kws_audit")
        fake_config.DIRECT_COMMANDS = {"音乐模式": []}
        with mock.patch.dict(sys.modules, {"config": fake_config}):
            with mock.patch(
                "core.utils.config.ConfigManager.instance",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertLogs("xiaozhi", level="WARNING") as captured:
                    self.assertEqual(audit_kws_coverage(), [])
        self.assertTrue(any("skipped" in line for line in captured.output))


class DeployConfigDataPlaneGuardTest(unittest.TestCase):
    """真实生产数据守门：deploy 表与词表当前零缺失（以后改表漂移即红）。"""

    def test_deploy_direct_commands_fully_covered(self):
        module = _load_deploy_config()
        keywords = module.APP_CONFIG["wakeup"]["keywords"]
        self.assertEqual(
            missing_kws_keywords(module.DIRECT_COMMANDS, keywords),
            [],
        )


class AppHookTest(unittest.TestCase):
    """core/app.py 挂点：开机直调 + reload listener 都落到 audit_kws_coverage。"""

    def test_reload_listener_hook_calls_audit(self):
        from core import app as app_module

        with mock.patch.object(app_module, "audit_kws_coverage") as audit:
            app_module.MainApp._on_config_reload_audit_kws({"old": {}}, {"new": {}})
        self.assertEqual(audit.call_count, 1)

    def test_run_registers_listener_and_audits_once(self):
        # run() 全流程太重；此处锁挂线契约：listener 注册与开机直调在 watcher 启动后
        source = (ROOT / "core" / "app.py").read_text(encoding="utf8")
        self.assertIn("add_reload_listener(self._on_config_reload_audit_kws)", source)
        self.assertIn("self._on_config_reload_audit_kws()", source)
        self.assertLess(
            source.index("_start_config_watcher()"),
            source.index("add_reload_listener(self._on_config_reload_audit_kws)"),
        )


if __name__ == "__main__":
    unittest.main()
