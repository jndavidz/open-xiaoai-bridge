"""xiaoai-tts skill 客户端鉴权测试（ticket 09）。

被测对象：`skills/xiaoai-tts/scripts/api_client.py`（std 库 urllib，无第三方依赖）。
所有 play/wakeup/control/tts 请求都经 `api_request`，因此在这一层断言即可覆盖全部分支；
`play_file.py` 走同一个 `require_token()` / `auth_headers()` helper。

覆盖：
  1. token 读取（去空白、未配置为空）与 header 构造
  2. 写类请求（POST play/wakeup/interrupt/tts）自动带 `Authorization: Bearer`；
     GET 只读端点（health/status/voices）不强制
  3. 未配置 token 时**发请求前**抛带修复提示的错误（信息不含 token 值）
  4. 显式 require_auth 覆写（只读端点也能强制带 token）

用法（纯 stdlib，无需网络/音箱）：
    python3 tests/test_skill_api_client.py
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = ROOT / "skills/xiaoai-tts/scripts/api_client.py"


def _load_client():
    spec = importlib.util.spec_from_file_location("xiaoai_tts_api_client", CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load_client()

TOKEN = "s3cr3t-skill-token"


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TokenHelpersTest(unittest.TestCase):
    def test_get_token_strips(self):
        self.assertEqual(client.get_token({client.TOKEN_ENV: "  abc  "}), "abc")
        self.assertEqual(client.get_token({}), "")
        self.assertEqual(client.get_token({client.TOKEN_ENV: "   "}), "")

    def test_auth_headers(self):
        self.assertEqual(client.auth_headers("abc"), {"Authorization": "Bearer abc"})
        # 空白 token 不构造头（避免发出 `Bearer ` 被服务端拒成 401）
        self.assertEqual(client.auth_headers("   "), {})

    def test_requires_auth_classification(self):
        for path in (
            "/api/play/text",
            "/api/play/url",
            "/api/play/file",
            "/api/wakeup",
            "/api/interrupt",
            "/api/tts/doubao",
        ):
            self.assertTrue(client.requires_auth(path, "POST"), path)
        # 识别文本 WS 属敏感读，服务端按写类处理
        self.assertTrue(client.requires_auth("/api/inject/text", "GET"))
        self.assertTrue(client.requires_auth("/api/status", "POST"))
        for path in ("/api/health", "/api/status", "/api/tts/doubao_voices"):
            self.assertFalse(client.requires_auth(path, "GET"), path)
        self.assertFalse(client.requires_auth("/api/status?x=1", "get"))


class MissingTokenTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(client.os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        client.os.environ.pop(client.TOKEN_ENV, None)

    def test_write_request_fails_before_network(self):
        with mock.patch.object(client.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(client.ApiAuthError) as ctx:
                client.api_request("/api/play/text", method="POST",
                                   data={"text": "你好"})
            urlopen.assert_not_called()
        message = str(ctx.exception)
        self.assertIn(client.TOKEN_ENV, message)
        self.assertIn("Authorization: Bearer", message)
        self.assertNotIn(TOKEN, message)

    def test_require_token_raises(self):
        with self.assertRaises(client.ApiAuthError):
            client.require_token()


class HeaderOnWireTest(unittest.TestCase):
    def setUp(self):
        client.os.environ[client.TOKEN_ENV] = TOKEN
        self.addCleanup(client.os.environ.pop, client.TOKEN_ENV, None)

    def _capture(self, payload=None):
        captured = {}

        def fake_urlopen(req, timeout=None):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["method"] = req.get_method()
            captured["data"] = req.data
            return _FakeResponse(payload or {"success": True})

        return captured, mock.patch.object(
            client.urllib.request, "urlopen", fake_urlopen
        )

    def test_post_play_text_carries_bearer(self):
        captured, patcher = self._capture()
        with patcher:
            client.api_request("/api/play/text", method="POST",
                               data={"text": "你好"})
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["data"].decode()), {"text": "你好"})

    def test_wakeup_and_interrupt_carry_bearer(self):
        captured, patcher = self._capture()
        with patcher:
            client.wakeup(silent=True)
            client.interrupt()
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers["authorization"], f"Bearer {TOKEN}")

    def test_read_endpoint_works_without_token(self):
        client.os.environ.pop(client.TOKEN_ENV, None)
        captured, patcher = self._capture({"success": True, "data": {}})
        with patcher:
            client.check_health()
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertNotIn("authorization", headers)

    def test_read_endpoint_attaches_token_when_present(self):
        captured, patcher = self._capture({"success": True, "data": {}})
        with patcher:
            client.get_status()
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers["authorization"], f"Bearer {TOKEN}")

    def test_require_auth_override(self):
        captured, patcher = self._capture()
        with patcher:
            client.api_request("/api/status", method="GET", require_auth=True)
        headers = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(headers["authorization"], f"Bearer {TOKEN}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
