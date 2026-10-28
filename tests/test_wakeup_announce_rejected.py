"""wakeup() 钩子异常收口测试（B-28 票 01 / 评审候选 S3）。

此前 before_wakeup 钩子的异常沿调用链穿透进事件循环，只剩一条
"exception was never retrieved"——用户看到「说了指令没反应」而无解释。
本测试守门三条语义：
  1. AnnounceRejected（词表拒绝）→ 不上抛、不进唤醒路由、kws 恢复、有 WARNING；
  2. 任意其他异常 → 不上抛、不进唤醒路由、kws 恢复、有 ERROR；
  3. 正常返回 None 照旧不路由（现行为不变）。
"""

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_event_manager(make_hook, order=None):
    """以桩依赖加载 core/wakeup_session.py，返回 (EventManager 类, fakes)。

    make_hook(rejected_cls) → 异步钩子；rejected_cls 是本次桩内的
    AnnounceRejected 类（wakeup_session 的 except 捕获的就是它，必须同源）。
    """
    saved = {
        n: sys.modules.get(n)
        for n in [
            "core.ref", "core.backend_registry", "core.services.protocols.typing",
            "core.services.speaker", "core.utils.config", "core.utils.logger",
            "core.tools", "core.tools.song_request",
        ]
    }
    for name in saved:
        sys.modules[name] = types.ModuleType(name)

    kws_calls: list[str] = []
    kws = types.SimpleNamespace(pause=lambda: kws_calls.append("pause"),
                                resume=lambda: kws_calls.append("resume"))
    sys.modules["core.ref"].get_app = lambda: None
    sys.modules["core.ref"].get_kws = lambda: kws
    sys.modules["core.ref"].get_speaker = lambda: None
    sys.modules["core.ref"].get_xiaozhi = lambda: None
    sys.modules["core.services.protocols.typing"].AbortReason = object
    sys.modules["core.services.speaker"].SpeakerManager = type("SpeakerManager", (), {})
    rejected_cls = type("AnnounceRejected", (RuntimeError,), {})
    sys.modules["core.services.speaker"].AnnounceRejected = rejected_cls
    routed: list[str] = []
    registry = types.SimpleNamespace(
        names=lambda: [],
        get=lambda name: (routed.append(name) if name else None, None)[1],  # 仅记录按后端名的路由查询
        reset_session_keys=lambda config: None,
    )
    sys.modules["core.backend_registry"].backend_registry = registry

    class _Log:
        def __init__(self):
            self.lines: list[tuple[str, str]] = []

        def _rec(self, level):
            def f(msg, *a, **k):
                self.lines.append((level, str(msg)))
            return f

        info = property(lambda self: self._rec("info"))
        warning = property(lambda self: self._rec("warning"))
        error = property(lambda self: self._rec("error"))
        debug = property(lambda self: self._rec("debug"))

    log = _Log()
    sys.modules["core.utils.logger"].logger = log

    # 在线点歌桩：唤醒必须在 before_wakeup（开场白会顶掉当前曲）之前暂停播放
    song_calls: list[str] = order if order is not None else []

    async def _pause_for_wakeup():
        song_calls.append("pause_for_wakeup")
        return False

    async def _resume_after_wakeup(reason="command"):
        song_calls.append("resume_after_wakeup")
        return False

    song_stub = types.SimpleNamespace(
        cancel_pending_resume=lambda: song_calls.append("cancel_resume"),
        pause_for_wakeup=_pause_for_wakeup,
        resume_after_wakeup=_resume_after_wakeup,
        is_paused_for_wakeup=lambda: False,
    )
    sys.modules["core.tools"].song_request = song_stub
    sys.modules["core.tools.song_request"] = song_stub

    class _Config:
        def get_app_config(self, path):
            assert path == "wakeup.before_wakeup"
            return make_hook(rejected_cls)

    sys.modules["core.utils.config"].ConfigManager = types.SimpleNamespace(
        instance=lambda: _Config()
    )

    spec = importlib.util.spec_from_file_location(
        "wakeup_under_test", ROOT / "core" / "wakeup_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        # 桩只在加载期需要；恢复现场避免污染同进程后续测试
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    return module.EventManager, {
        "kws_calls": kws_calls,
        "routed": routed,
        "log": log,
        "rejected_cls": rejected_cls,
        "song_calls": song_calls,
        "song_stub": song_stub,
    }


class WakeupHookErrorHandlingTest(unittest.TestCase):
    """wakeup() 对 before_wakeup 异常的收口语义。"""

    def test_announce_rejected_is_swallowed_and_routed_nowhere(self):
        def make_hook(rejected):
            async def hook(speaker, text, source, app):
                raise rejected("高保真模式")
            return hook

        EM, fakes = _load_event_manager(make_hook)
        asyncio.run(EM.wakeup("音乐模式", "kws"))

        self.assertEqual(fakes["routed"], [], "拒绝后不进唤醒路由")
        self.assertEqual(fakes["kws_calls"], ["pause", "resume"], "kws 会话恢复")
        self.assertTrue(
            any(lv == "warning" and "词表拒绝" in msg for lv, msg in fakes["log"].lines),
            "拒绝必须有 user 可见的 WARNING 日志",
        )

    def test_generic_hook_error_is_swallowed_and_logged(self):
        def make_hook(rejected):
            async def hook(speaker, text, source, app):
                raise ValueError("boom")
            return hook

        EM, fakes = _load_event_manager(make_hook)
        asyncio.run(EM.wakeup("随便说", "xiaoai"))

        self.assertEqual(fakes["routed"], [])
        self.assertEqual(fakes["kws_calls"], ["pause", "resume"])
        self.assertTrue(
            any(lv == "error" and "before_wakeup hook failed" in msg
                for lv, msg in fakes["log"].lines),
            "任意钩子异常必须有 ERROR 日志",
        )

    def test_none_return_keeps_no_route(self):
        def make_hook(rejected):
            async def hook(speaker, text, source, app):
                return None
            return hook

        EM, fakes = _load_event_manager(make_hook)
        asyncio.run(EM.wakeup("你好", "kws"))

        self.assertEqual(fakes["routed"], [], "None 不路由（现行为不变）")
        self.assertEqual(fakes["kws_calls"], ["pause", "resume"])

    def test_song_paused_before_intro_hook(self):
        """开场白会顶掉当前曲 ⇒ 必须在 before_wakeup 之前暂停点歌（才续得回来）。

        钩子返回 None 即可验证先后顺序（生产里开场白就在这个钩子里播）。
        """
        order: list[str] = []

        def make_hook(rejected):
            async def hook(speaker, text, source, app):
                order.append("hook")
                return None
            return hook

        EM, fakes = _load_event_manager(make_hook, order=order)
        # 桩在加载期结束后被还原；wakeup() 里是运行期 import，故调用前重新注入
        prev_tools = sys.modules.get("core.tools")
        prev_song = sys.modules.get("core.tools.song_request")
        tools_mod = types.ModuleType("core.tools")
        tools_mod.song_request = fakes["song_stub"]
        sys.modules["core.tools"] = tools_mod
        sys.modules["core.tools.song_request"] = fakes["song_stub"]
        try:
            asyncio.run(EM.wakeup("你好小塔", "kws"))
        finally:
            for key, prev in (
                ("core.tools", prev_tools),
                ("core.tools.song_request", prev_song),
            ):
                if prev is None:
                    sys.modules.pop(key, None)
                else:
                    sys.modules[key] = prev

        self.assertEqual(
            ["cancel_resume", "pause_for_wakeup", "hook"],
            order,
            "暂停必须发生在 before_wakeup（开场白）之前",
        )


if __name__ == "__main__":
    unittest.main()
