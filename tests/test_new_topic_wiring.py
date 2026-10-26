"""「换个话题」接线测试（spec topic-switch-and-memory 票 03）。

覆盖：模板钩子默认不识别；OpenAI 控制器命中 → reset+置信令+播过渡语
（原子次序）且不发 LLM；未命中 / enabled=false 直通。
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

from core.external_conversation import ExternalConversationController  # noqa: E402
from core.openai_conversation import OpenAIConversationController  # noqa: E402
from core.utils.new_topic import match_new_topic  # noqa: E402

NEW_TOPIC_CFG = {
    "enabled": True,
    "exact": ["我们换个话题"],
    "fuzzy": ["换个话题", "聊点别的", "换个话题吧"],
    "transition": "好的，我们聊点别的。想聊什么？",
}


def _run(coro):
    return asyncio.run(coro)


class TemplateHookDefaultTest(unittest.TestCase):
    def test_base_controller_never_matches(self):
        """模板钩子基类默认 False——openclaw/qwenpaw/xiaozhi 路径不受影响。"""

        class Bare(ExternalConversationController):
            MANAGER = None

        controller = Bare.__new__(Bare)
        self.assertFalse(_run(controller._maybe_handle_new_topic("我们换个话题")))


class OpenAINewTopicTest(unittest.TestCase):
    def _controller(self, cfg, backend_calls, bypass_calls):
        controller = OpenAIConversationController.__new__(OpenAIConversationController)
        controller.LOG_MODULE = "OpenAI Conv"
        controller.config = types.SimpleNamespace(
            get_app_config=lambda path, default=None: cfg if path == "openai.new_topic" else default
        )
        controller.backend = types.SimpleNamespace(
            reset_session=lambda: backend_calls.append("reset"),
            force_new_session=lambda: backend_calls.append("flag"),
        )

        async def fake_bypass(text, *, reason):
            bypass_calls.append((text, reason))

        controller._play_bypass_text = fake_bypass
        return controller

    def test_hit_resets_and_signals_atomically_then_plays_transition(self):
        backend_calls: list[str] = []
        bypass_calls: list[tuple] = []
        controller = self._controller(NEW_TOPIC_CFG, backend_calls, bypass_calls)

        with mock.patch("core.openai_conversation.turn_trace.abandon_turn") as abandon:
            handled = _run(controller._maybe_handle_new_topic("我们换个话题吧"))

        self.assertTrue(handled)
        # 原子次序：先清历史（reset）后置信令（flag）——顺序敏感断言
        self.assertEqual(["reset", "flag"], backend_calls)
        self.assertEqual(1, abandon.call_count, "过渡轮 TurnTrace 收尾为 incomplete")
        self.assertEqual(
            [("好的，我们聊点别的。想聊什么？", "new topic transition")], bypass_calls
        )

    def test_fuzzy_variant_hit(self):
        backend_calls: list[str] = []
        controller = self._controller(NEW_TOPIC_CFG, backend_calls, [])

        async def noop(text, *, reason):
            pass

        controller._play_bypass_text = noop
        self.assertTrue(_run(controller._maybe_handle_new_topic("聊点别的吧")))
        self.assertEqual(["reset", "flag"], backend_calls)

    def test_long_sentence_containing_passphrase_not_intercepted(self):
        controller = self._controller(NEW_TOPIC_CFG, [], [])
        self.assertFalse(_run(controller._maybe_handle_new_topic("我想给你讲个换话题的笑话")))

    def test_disabled_config_passes_through(self):
        cfg = dict(NEW_TOPIC_CFG, enabled=False)
        controller = self._controller(cfg, [], [])
        self.assertFalse(_run(controller._maybe_handle_new_topic("我们换个话题")))

    def test_missing_config_passes_through(self):
        controller = self._controller(None, [], [])
        self.assertFalse(_run(controller._maybe_handle_new_topic("我们换个话题")))

    def test_matcher_contract_shared_with_pure_function(self):
        """接线用的识别语义与纯函数同源（防两处口径漂移）。"""
        self.assertTrue(match_new_topic("换个画图", exact=NEW_TOPIC_CFG["exact"], fuzzy=NEW_TOPIC_CFG["fuzzy"]))


if __name__ == "__main__":
    unittest.main()
