"""指令层内核化（P2 ticket 01）的纯内存契约测试。

被测对象 = core/commands/（executor + router + actions），全部用内存 fake
（fake speaker / fake app / 打桩 hass_action），不连设备、不联网、不触 Rust 扩展。

覆盖（对应 ticket 01 验收项 2/3）：
  1. 三型动作混排次序（str 播报 / ha 元组 / callable 按声明顺序执行，callable 收到 speaker）；
  2. 失败传播：str 播报或 callable 抛异常 → 中止后续步骤并上抛（默认拒绝）；
     HA 调用失败（hass_action 吞异常返 None）不中断序列；
  3. 清场→播报→执行→收尾：xiaoai 截胡先 stop_device_audio 再 run_steps，返回 None；
  4. 路由语义：kws 表命中执行不进对话；「停止聆听」静音+停听+播报；
     人设子串切换会话返回 backend；xiaoai 未命中放行（不清场）；未知 source 返回 None；
  5. deploy/config.py 数据面契约：两张表全部步骤是执行器认识的三型之一，
     钩子绑定走 core.commands.route_before_wakeup（数据面不再含执行逻辑）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from core import ref  # noqa: E402
from core.commands import route_before_wakeup, run_steps  # noqa: E402
from core.commands.router import STOP_LISTENING_KEYWORD  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _load_deploy_config(module_name: str = "deploy_config_under_test"):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "deploy" / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSpeaker:
    """内存音箱替身：记录调用时序，announce 以 async context manager 形态工作。"""

    def __init__(self, announce_error: Exception | None = None):
        self.calls: list[tuple] = []
        self.play_kwargs: list[dict] = []
        self.mic = True
        self.announce_error = announce_error

    def announce(self, *, text, reason, wait, gate_seconds):
        self.calls.append(("announce:open", text, reason))
        if self.announce_error is not None:
            raise self.announce_error
        return _AnnounceCtx(self, text)

    async def play(self, text="", **kwargs):
        self.calls.append(("play", text))
        self.play_kwargs.append(dict(kwargs))

    async def set_mic(self, enabled):
        self.mic = enabled
        self.calls.append(("set_mic", enabled))

    async def stop_device_audio(self):
        self.calls.append(("stop_device_audio",))


class _AnnounceCtx:
    """announce 上下文：正常退出记 close；body 异常向上传播（闸门归还在真实现里）。"""

    def __init__(self, speaker: FakeSpeaker, text: str):
        self._speaker = speaker
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._speaker.calls.append(("announce:close", self._text))
        return False


class FakeApp:
    """MainApp 替身：记录人设切换调用（B-28 票 03 起 router 走泛化门面）。"""

    def __init__(self):
        self.session_keys: list[str] = []
        self.session_backends: list[str] = []
        self.prompts: list[object] = []

    def set_session_key(self, backend, key):
        self.session_backends.append(backend)
        self.session_keys.append(key)

    def set_openai_system_prompt(self, prompt):
        self.prompts.append(prompt)


class FakeKWS:
    def __init__(self):
        self.disabled = False

    def disable_listening(self):
        self.disabled = True


# ---------------------------------------------------------------- run_steps 执行器契约


class RunStepsContractTest(unittest.TestCase):
    def test_mixed_step_types_run_in_declared_order(self) -> None:
        speaker = FakeSpeaker()
        ha_calls: list[tuple] = []
        callable_calls: list[object] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        async def _callable_step(sp):
            callable_calls.append(sp)

        steps = ["正在准备客厅音响", ("ha", "script", "turn_on", {"entity_id": "script.hifi_mode"}), _callable_step]
        with mock.patch("core.commands.hass.hass_action", _fake_hass):
            _run(run_steps(speaker, steps))

        self.assertEqual(
            [c[0] for c in speaker.calls],
            ["announce:open", "play", "announce:close"],
            "str 步骤 = 开 announce 闸门 → 播报 → 关闸门",
        )
        self.assertEqual(speaker.calls[0][1], "正在准备客厅音响")
        self.assertEqual(ha_calls, [("script", "turn_on", {"entity_id": "script.hifi_mode"})])
        self.assertEqual(callable_calls, [speaker], "callable 步骤收到 speaker")

    def test_announce_failure_propagates_and_aborts_remaining_steps(self) -> None:
        speaker = FakeSpeaker(announce_error=RuntimeError("词表命中拒绝"))
        ha_calls: list[tuple] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        steps = ["正在准备客厅音响", ("ha", "script", "turn_on", {})]
        with mock.patch("core.commands.hass.hass_action", _fake_hass):
            with self.assertRaises(RuntimeError):
                _run(run_steps(speaker, steps))

        self.assertEqual(ha_calls, [], "被拒步骤之后的动作不得执行")

    def test_callable_failure_propagates_and_aborts_remaining_steps(self) -> None:
        speaker = FakeSpeaker()
        ha_calls: list[tuple] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        async def _boom(sp):
            raise ValueError("boom")

        async def _after(sp):
            raise AssertionError("不应执行")

        steps = [("ha", "script", "turn_on", {}), _boom, _after]
        with mock.patch("core.commands.hass.hass_action", _fake_hass):
            with self.assertRaises(ValueError):
                _run(run_steps(speaker, steps))

        self.assertEqual(ha_calls, [("script", "turn_on", {})], "失败步骤之前的动作已执行")

    def test_ha_failure_does_not_stop_sequence(self) -> None:
        # hass_action 自吞异常返 None（尽力而为语义）：单个 HA 调用失败
        # 不应中断后续播报/收尾步骤。
        speaker = FakeSpeaker()
        callable_calls: list[object] = []

        async def _failing_hass(domain, service, data=None):
            return None  # 模拟 HA 超时/网络失败

        async def _callable_step(sp):
            callable_calls.append(sp)

        steps = [("ha", "script", "turn_on", {}), "已为您处理", _callable_step]
        with mock.patch("core.commands.hass.hass_action", _failing_hass):
            _run(run_steps(speaker, steps))

        self.assertEqual([c[0] for c in speaker.calls], ["announce:open", "play", "announce:close"])
        self.assertEqual(callable_calls, [speaker])


# ---------------------------------------------------------------- 路由契约


class RouterContractTest(unittest.TestCase):
    def setUp(self):
        self.speaker = FakeSpeaker()
        self.app = FakeApp()
        self.direct = {
            "下一首歌曲": [("ha", "script", "turn_on", {"entity_id": "script.music_next"})],
        }
        self.xiaoai = {"打开电脑": ["正在唤醒客厅电脑", ("ha", "switch", "turn_on", {"entity_id": "switch.wol"})]}
        self.personas = {
            "你好老师": {"session_key": "agent:tutor:home", "system_prompt": "TUTOR", "intro": "老师在，请讲", "backend": "openai"},
            "贾维斯": {"session_key": "agent:javis:home", "system_prompt": None, "intro": "我在", "backend": "openai"},
        }

    def _route(self, text, source):
        return _run(route_before_wakeup(
            self.speaker, text, source, self.app,
            direct_table=self.direct,
            xiaoai_table=self.xiaoai,
            personas=self.personas,
        ))

    def test_direct_command_hit_executes_and_stays_out_of_conversation(self) -> None:
        ha_calls: list[tuple] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        with mock.patch("core.commands.hass.hass_action", _fake_hass):
            result = self._route("下一首歌曲", "kws")

        self.assertIsNone(result, "命中免唤醒指令后不进入 AI 连续对话")
        self.assertEqual(ha_calls, [("script", "turn_on", {"entity_id": "script.music_next"})])
        self.assertNotIn(("stop_device_audio",), self.speaker.calls, "kws 路径不清场")

    def test_xiaoai_intercept_clears_stage_then_runs_steps(self) -> None:
        ha_calls: list[tuple] = []
        callable_calls: list[object] = []

        async def _fake_hass(domain, service, data=None):
            ha_calls.append((domain, service, data))
            return 200

        async def _callable_step(sp):
            callable_calls.append(sp)

        self.xiaoai["打开电脑"] = ["正在唤醒客厅电脑", ("ha", "switch", "turn_on", {"entity_id": "switch.wol"}), _callable_step]
        with mock.patch("core.commands.hass.hass_action", _fake_hass):
            result = self._route("打开电脑", "xiaoai")

        self.assertIsNone(result, "截胡执行完毕不进原生小爱")
        kinds = [c[0] for c in self.speaker.calls]
        self.assertEqual(
            kinds,
            ["stop_device_audio", "announce:open", "play", "announce:close"],
            "清场 → 播报 → 执行 → 收尾",
        )
        self.assertEqual(callable_calls, [self.speaker])

    def test_xiaoai_miss_passes_through_without_clearing(self) -> None:
        result = self._route("把空调开到26度", "xiaoai")
        self.assertIsNone(result, "未命中放行原生小爱")
        self.assertEqual(self.speaker.calls, [], "放行路径零副作用（不清场）")

    def test_kws_miss_returns_none_without_side_effects(self) -> None:
        result = self._route("不在表里的话", "kws")
        self.assertIsNone(result)
        self.assertEqual(self.speaker.calls, [])

    def test_stop_listening_mutes_mic_and_disables_kws(self) -> None:
        kws = FakeKWS()
        ref.set_kws(kws)
        try:
            result = self._route(STOP_LISTENING_KEYWORD, "kws")
        finally:
            ref.set_kws(None)

        self.assertIsNone(result)
        self.assertFalse(self.speaker.mic, "硬件麦克风已静音")
        self.assertTrue(kws.disabled, "KWS 持久停止分析")
        self.assertEqual(
            [c[0] for c in self.speaker.calls],
            ["set_mic", "play"],
            "先静音停听再播报确认",
        )
        self.assertIn(("play", "已停止聆听"), self.speaker.calls)

    def test_persona_switch_sets_session_and_prompt(self) -> None:
        result = self._route("你好老师讲个题", "kws")

        self.assertEqual(result, "openai", "人设命中进入 AI 连续对话")
        self.assertEqual(self.app.session_keys, ["agent:tutor:home"])
        self.assertEqual(self.app.prompts, ["TUTOR"])
        self.assertIn(("play", "老师在，请讲"), self.speaker.calls)

    def test_persona_reset_clears_prompt_override(self) -> None:
        result = self._route("嗨贾维斯", "kws")

        self.assertEqual(result, "openai")
        self.assertEqual(self.app.session_keys, ["agent:javis:home"])
        self.assertEqual(self.app.prompts, [None], "切回贾维斯显式复位人设覆盖")
        self.assertIn(("play", "我在"), self.speaker.calls)

    def test_persona_matching_is_substring_and_order_sensitive(self) -> None:
        # 声明顺序 = 匹配优先级：文本同时含两个人设词时先声明者胜
        self.personas = {
            "老师": {"session_key": "agent:tutor:home", "system_prompt": "T", "intro": "", "backend": "openai"},
            "贾维斯": {"session_key": "agent:javis:home", "system_prompt": None, "intro": "", "backend": "openai"},
        }
        result = self._route("贾维斯老师", "kws")
        self.assertEqual(result, "openai")
        self.assertEqual(self.app.session_keys, ["agent:tutor:home"])

    def test_persona_without_session_key_routes_other_backend_only(self) -> None:
        """B-28 票 01：无 session_key 的人设条目 = 纯路由（多后端唤醒词路由），
        不写 openai 的会话态；intro 仍经 announce 播出。"""
        self.personas = {
            "龙虾": {"backend": "openclaw", "intro": "龙虾来了"},
        }
        result = self._route("你好龙虾", "kws")

        self.assertEqual(result, "openclaw")
        self.assertEqual(self.app.session_keys, [], "无 session_key 不写 openai 会话态")
        self.assertEqual(self.app.prompts, [])
        self.assertEqual(
            [c[0] for c in self.speaker.calls],
            ["announce:open", "play", "announce:close"],
            "intro 经 announce 双闸门",
        )
        # 开场白完成等待必须有硬上限：默认按文本估算的最小预算是 8s，事件面冷启动
        # 时观测不到播放事件 ⇒ 白等 8s（真机 2026-10-28 唤醒到聆听卡 8 秒）
        self.assertEqual(3000, self.speaker.play_kwargs[-1].get("timeout"))

    def test_persona_intro_rejected_by_wordlist_propagates(self) -> None:
        """B-28 票 01：intro 命中词表 → announce 默认拒绝上抛（由 wakeup 收口为可见日志），
        不播报、不路由。异常类本地定义即可：FakeSpeaker 抛什么、断言就捕什么。"""

        class AnnounceRejected(RuntimeError):
            pass

        self.personas = {
            "龙虾": {"backend": "openclaw", "intro": "高保真模式已开启"},
        }
        self.speaker.announce_error = AnnounceRejected("高保真模式")

        with self.assertRaises(AnnounceRejected):
            self._route("你好龙虾", "kws")
        self.assertNotIn(("play", "高保真模式已开启"), self.speaker.calls)

    def test_unknown_source_returns_none(self) -> None:
        result = self._route("任意文本", "xiaozhi")
        self.assertIsNone(result)
        self.assertEqual(self.speaker.calls, [])


# ---------------------------------------------------------------- deploy/config.py 数据面契约


class DeployConfigDataContractTest(unittest.TestCase):
    """数据面守门：config 表数据必须是执行器认识的三型之一，钩子必须走 core。"""

    @classmethod
    def setUpClass(cls):
        cls.deploy = _load_deploy_config()

    def test_command_table_steps_are_supported_types(self) -> None:
        for table in (self.deploy.DIRECT_COMMANDS, self.deploy.XIAOAI_COMMANDS):
            for key, steps in table.items():
                self.assertIsInstance(steps, list, f"{key} 的步骤必须是 list")
                for step in steps:
                    ok = (
                        isinstance(step, str)
                        or (isinstance(step, tuple) and step[:1] == ("ha",))
                        or callable(step)
                    )
                    self.assertTrue(ok, f"{key} 含执行器不认识的步骤类型: {type(step)}")

    def test_hooks_delegate_to_core_router(self) -> None:
        # 路由钩子 = 数据绑定层：函数体里不允许出现执行逻辑（announce/hass/run 等）
        import inspect

        source = inspect.getsource(self.deploy.before_wakeup)
        for forbidden in ("announce", "hass_action", "run_steps", "stop_device_audio", "set_mic", "get_kws"):
            self.assertNotIn(forbidden, source, f"路由钩子不得包含执行逻辑: {forbidden}")
        self.assertIn("route_before_wakeup", source, "钩子应委托 core.commands.route_before_wakeup")

    def test_config_no_longer_imports_core_ref(self) -> None:
        with open(ROOT / "deploy" / "config.py", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("from core.ref", source, "数据面不得反向 import 核心单例注册表")
        self.assertNotIn("get_kws()", source, "数据面不得直接操作 KWS 单例")


if __name__ == "__main__":
    unittest.main(verbosity=2)
