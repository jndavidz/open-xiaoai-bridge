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


if __name__ == "__main__":
    unittest.main()
