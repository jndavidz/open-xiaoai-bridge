import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OpenAIHeadersTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        sys.modules.setdefault("requests", types.SimpleNamespace(post=None, get=None))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.openai", None)
        self.manager = importlib.import_module("core.openai").OpenAIManager
        self.manager._api_key = ""
        self.manager._session_key = "agent:default:open-xiaoai-bridge"

    def test_default_sends_hermes_session_header(self):
        """Default config targets Hermes: session_key goes out as the header."""
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            self.manager._headers()["X-Hermes-Session-Key"],
        )

    def test_empty_session_header_disables_it(self):
        """Setting session_header empty keeps requests header-free (plain OpenAI)."""
        self.manager._session_header = ""
        self.assertEqual({"Content-Type": "application/json"}, self.manager._headers())

    def test_session_header_sent_when_configured(self):
        self.manager._session_header = "X-Hermes-Session-Key"
        headers = self.manager._headers()
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            headers["X-Hermes-Session-Key"],
        )

    def test_session_header_omitted_when_session_key_empty(self):
        self.manager._session_header = "X-Hermes-Session-Key"
        self.manager._session_key = ""
        self.assertNotIn("X-Hermes-Session-Key", self.manager._headers())

    def test_bearer_auth_and_session_header_coexist(self):
        self.manager._api_key = "secret"
        self.manager._session_header = "X-Hermes-Session-Key"
        headers = self.manager._headers()
        self.assertEqual("Bearer secret", headers["Authorization"])
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            headers["X-Hermes-Session-Key"],
        )

    def test_x_session_key_overlaid_alongside_session_header(self):
        """B-30 票 01：会话头机制开启时叠加 X-Session-Key（aurora 会话复用读法）。"""
        self.manager._session_header = "X-Hermes-Session-Key"
        headers = self.manager._headers()
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            headers["X-Session-Key"],
            "与 session_header 并存同值（aurora 读前者）",
        )

    def test_x_session_key_idempotent_when_already_session_header(self):
        """生产 aurora 配置 session_header=X-Session-Key：叠加为幂等回写。"""
        self.manager._session_header = "X-Session-Key"
        headers = self.manager._headers()
        self.assertEqual("agent:default:open-xiaoai-bridge", headers["X-Session-Key"])
        self.assertEqual(1, list(headers).count("X-Session-Key"))

    def test_force_new_session_signal_is_one_shot(self):
        """B-30 票 01：置位 → 首次 headers 消费带信令并自动清位 → 第二次无信令。"""
        self.manager._session_header = "X-Session-Key"
        self.manager.force_new_session()
        first = self.manager._headers()
        self.assertEqual("new", first.get("X-Session-Action"))
        self.assertFalse(self.manager._force_new_session, "one-shot：消费即清位")
        second = self.manager._headers()
        self.assertNotIn("X-Session-Action", second)

    def test_new_session_signal_absent_by_default(self):
        self.manager._session_header = "X-Session-Key"
        self.assertNotIn("X-Session-Action", self.manager._headers())


class OpenAIMemoryTest(unittest.TestCase):
    """对话记忆接线（B-30 票 04）：写入钩子 / 注入触发条件 / 降级。"""

    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        sys.modules.setdefault("requests", types.SimpleNamespace(post=None, get=None))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.openai", None)
        self.manager = importlib.import_module("core.openai").OpenAIManager
        self.manager._api_key = ""
        self.manager._session_key = "agent:default:open-xiaoai-bridge"
        self.manager._session_header = ""
        self.manager._history_max_messages = 20

        import tempfile
        from datetime import datetime

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from pathlib import Path

        self.memory_dir = Path(self._tmp.name)
        self.manager._memory_dir = self.memory_dir
        self.manager._memory_config = {"enabled": True, "max_turns": 5, "gap_minutes": 30}
        self.manager._last_response_at = None
        self.addCleanup(setattr, self.manager, "_last_response_at", None)
        self.now = datetime.now()

    def test_response_complete_appends_memory(self):
        self.manager._on_response_complete("你好", "你好呀")
        self.assertIsNotNone(self.manager._last_response_at)
        from core.services import conversation_memory as cm

        turns = cm.recent_turns(self.memory_dir, self.manager._session_key, limit=5, now=self.now)
        self.assertEqual(1, len(turns))
        self.assertEqual("你好", turns[0]["user"])

    def test_memory_disabled_no_write(self):
        self.manager._memory_config = {"enabled": False}
        self.manager._on_response_complete("你好", "你好呀")
        self.assertIsNone(self.manager._last_response_at)
        self.assertEqual([], list(self.memory_dir.glob("*.jsonl")))

    def test_inject_triggers_after_gap(self):
        from core.services import conversation_memory as cm

        cm.append_turn(self.memory_dir, self.manager._session_key, "昨天聊了天气", "好的", self.now)
        self.manager._last_response_at = None  # 断档（容器重启/超 gap）
        messages = self.manager._build_messages([], "今天呢")
        self.assertEqual(2, len(messages))
        self.assertTrue(any("此前对话摘要" in m["content"] for m in messages), "断档触发注入摘要")
        self.assertIn("system", [m["role"] for m in messages])

    def test_inject_triggers_on_new_topic_flag(self):
        from core.services import conversation_memory as cm
        import time as time_mod

        cm.append_turn(self.memory_dir, self.manager._session_key, "聊过了", "嗯", self.now)
        self.manager._last_response_at = time_mod.time()  # 刚聊过（无 gap）
        self.manager._force_new_session = True  # 刚换话题
        self.addCleanup(setattr, self.manager, "_force_new_session", False)
        messages = self.manager._build_messages([], "新话题")
        self.assertEqual(2, len(messages))
        self.assertTrue(any("此前对话摘要" in m["content"] for m in messages), "换话题触发注入摘要")

    def test_no_inject_in_active_session(self):
        import time as time_mod

        self.manager._last_response_at = time_mod.time()  # 刚聊过且无换话题
        messages = self.manager._build_messages([], "继续")
        self.assertEqual(1, len(messages))  # 只有 user 消息，无摘要

    def test_inject_degrades_gracefully_when_dir_broken(self):
        from pathlib import Path

        self.manager._memory_dir = Path(self._tmp.name) / "blocker"
        self.memory_dir.joinpath("blocker").write_text("x", encoding="utf-8")
        self.manager._last_response_at = None  # 断档触发
        messages = self.manager._build_messages([], "今天呢")
        self.assertEqual(1, len(messages), "读取失败降级为无摘要，不抛")


if __name__ == "__main__":
    unittest.main()
