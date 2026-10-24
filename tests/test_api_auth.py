"""业务 API 鉴权纯函数测试（ticket 06：业务 API 鉴权 + client token 失败关闭）。

不启动 aiohttp、不连音箱、不加载任何重依赖：只测 `core/services/auth.py` 的纯逻辑。

覆盖：
  1. 路径分类：health / read / write（含「未登记即按写类失败关闭」与敏感读）
  2. Bearer 头解析：缺失、空、错误 scheme、空凭据、大小写、多余空白
  3. 判定：正确 token 放行；缺失 401；错误 403；未配置 503（附配置提示）
  4. 管理面板 token（ADMIN_TOKEN）与业务 token 分离，互不回退
  5. 错误文案不含 token 明文，也不含服务端内部路径

用法：
    python3 tests/test_api_auth.py
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.services.auth import (  # noqa: E402
    ENV_TOKEN,
    KIND_HEALTH,
    KIND_READ,
    KIND_WRITE,
    authorize,
    classify_route,
    configured_token,
    parse_bearer,
)

TOKEN = "s3cr3t-business-token"


class RouteClassificationTest(unittest.TestCase):
    """路径分类：决定「未配置 token 时哪些能过」。"""

    def test_health_is_open(self):
        self.assertEqual(classify_route("/api/health", "GET"), KIND_HEALTH)
        self.assertEqual(classify_route("/api/health/", "get"), KIND_HEALTH)
        self.assertEqual(classify_route("/api/health?probe=1", "HEAD"), KIND_HEALTH)

    def test_status_is_read(self):
        self.assertEqual(classify_route("/api/status", "GET"), KIND_READ)
        self.assertEqual(classify_route("/api/status", "get"), KIND_READ)
        self.assertEqual(classify_route("/api/status/", "GET"), KIND_READ)

    def test_inject_status_is_read_but_text_stream_is_write(self):
        # 闸门状态只回 open/closed + 剩余秒数 → 读类
        self.assertEqual(classify_route("/api/inject", "GET"), KIND_READ)
        # 识别文本是语音内容（敏感读）→ 必须带 token，按写类处理
        self.assertEqual(classify_route("/api/inject/text", "GET"), KIND_WRITE)

    def test_write_endpoints(self):
        for path in (
            "/api/play/text",
            "/api/play/url",
            "/api/play/file",
            "/api/wakeup",
            "/api/interrupt",
            "/api/audio_input",
            "/api/inject",
            "/api/inject/stream",
            "/api/tts/doubao",
        ):
            self.assertEqual(classify_route(path, "POST"), KIND_WRITE, path)

    def test_safe_method_is_required_for_read(self):
        # 同一路径的非安全方法（POST /api/status）不得被当成读类
        self.assertEqual(classify_route("/api/status", "POST"), KIND_WRITE)
        self.assertEqual(classify_route("/api/inject/relay", "DELETE"), KIND_WRITE)

    def test_unknown_route_fails_closed(self):
        # 未登记路径按写类处理：鉴权先于 404，不泄露路由是否存在
        self.assertEqual(classify_route("/api/unknown", "GET"), KIND_WRITE)
        self.assertEqual(classify_route("/api/unknown", "POST"), KIND_WRITE)

    def test_admin_paths_are_not_health_or_read(self):
        # 管理面板由 ADMIN_TOKEN 中间件负责；这里不得放行
        self.assertEqual(classify_route("/api/admin/overview", "GET"), KIND_WRITE)
        self.assertEqual(classify_route("/admin", "GET"), KIND_WRITE)


class ParseBearerTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_bearer("Bearer abc"), "abc")
        self.assertEqual(parse_bearer("bearer abc"), "abc")
        self.assertEqual(parse_bearer("BEARER abc"), "abc")
        self.assertEqual(parse_bearer("  Bearer   abc  "), "abc")

    def test_missing_and_empty(self):
        self.assertIsNone(parse_bearer(None))
        self.assertIsNone(parse_bearer(""))
        self.assertIsNone(parse_bearer("   "))
        self.assertIsNone(parse_bearer("Bearer"))
        self.assertIsNone(parse_bearer("Bearer   "))

    def test_wrong_scheme(self):
        self.assertIsNone(parse_bearer("Basic abc"))
        self.assertIsNone(parse_bearer("Token abc"))
        self.assertIsNone(parse_bearer("abc"))

    def test_extra_parts_rejected(self):
        self.assertIsNone(parse_bearer("Bearer a b"))


class AuthorizeTest(unittest.TestCase):
    """判定函数：这是中间件的唯一决策点。"""

    def test_health_and_read_always_allowed(self):
        for path in ("/api/health", "/api/status", "/api/inject", "/api/inject/relay"):
            d = authorize(path, "GET", authorization=None, expected_token="")
            self.assertTrue(d.allowed, path)
            self.assertEqual(d.status, 200, path)
            # 未配置 token 也要能读；带错 token 也不影响只读端点
            d = authorize(path, "GET", authorization="Bearer wrong", expected_token=TOKEN)
            self.assertTrue(d.allowed, path)

    def test_write_allowed_with_correct_token(self):
        d = authorize(
            "/api/play/text",
            "POST",
            authorization=f"Bearer {TOKEN}",
            expected_token=TOKEN,
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.status, 200)
        self.assertEqual(d.code, "ok")

    def test_write_without_header_is_401(self):
        d = authorize("/api/play/text", "POST", authorization=None, expected_token=TOKEN)
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 401)
        self.assertEqual(d.code, "token_missing")
        # 401 应提示客户端用 Bearer
        self.assertIn(("WWW-Authenticate", "Bearer"), d.headers)

    def test_write_with_wrong_scheme_is_401(self):
        for header in ("Basic " + TOKEN, TOKEN, "Bearer", "Bearer   "):
            d = authorize(
                "/api/wakeup", "POST", authorization=header, expected_token=TOKEN
            )
            self.assertFalse(d.allowed, header)
            self.assertEqual(d.status, 401, header)
            self.assertEqual(d.code, "token_missing", header)

    def test_write_with_wrong_token_is_403(self):
        d = authorize(
            "/api/interrupt",
            "POST",
            authorization="Bearer not-the-token",
            expected_token=TOKEN,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 403)
        self.assertEqual(d.code, "token_invalid")
        self.assertEqual(d.headers, ())

    def test_unconfigured_write_is_503_with_hint(self):
        for path, method in (
            ("/api/play/text", "POST"),
            ("/api/wakeup", "POST"),
            ("/api/audio_input", "POST"),
            ("/api/inject", "POST"),
            ("/api/inject/stream", "POST"),
        ):
            d = authorize(path, method, authorization=None, expected_token="")
            self.assertFalse(d.allowed, path)
            self.assertEqual(d.status, 503, path)
            self.assertEqual(d.code, "auth_unconfigured", path)
            # 必须给出「如何配置」，且只给变量名
            self.assertIn(ENV_TOKEN, d.error, path)
            self.assertIn("Authorization: Bearer", d.error, path)

    def test_non_ascii_token_does_not_crash(self):
        # hmac.compare_digest 对非 ASCII str 会抛 TypeError —— 必须按 bytes 比较
        d = authorize(
            "/api/play/text",
            "POST",
            authorization="Bearer 口令",
            expected_token=TOKEN,
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 403)

    def test_errors_do_not_leak_token_or_paths(self):
        decisions = [
            authorize("/api/play/text", "POST", authorization=None, expected_token=TOKEN),
            authorize(
                "/api/play/text",
                "POST",
                authorization="Bearer wrong",
                expected_token=TOKEN,
            ),
            authorize("/api/play/text", "POST", authorization=None, expected_token=""),
        ]
        for d in decisions:
            self.assertNotIn(TOKEN, d.error)
            self.assertNotIn("wrong", d.error)
            for leak in ("/app", "/mnt", "C:\\", "/data"):
                self.assertNotIn(leak, d.error, (d.code, leak))


class ConfiguredTokenTest(unittest.TestCase):
    def setUp(self):
        self._saved = {
            ENV_TOKEN: os.environ.get(ENV_TOKEN),
            "ADMIN_TOKEN": os.environ.get("ADMIN_TOKEN"),
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_reads_and_strips(self):
        os.environ[ENV_TOKEN] = "  abc  "
        self.assertEqual(configured_token(), "abc")

    def test_unset_or_blank_is_empty(self):
        os.environ.pop(ENV_TOKEN, None)
        self.assertEqual(configured_token(), "")
        os.environ[ENV_TOKEN] = "   "
        self.assertEqual(configured_token(), "")

    def test_admin_token_is_not_used(self):
        # token 分离：只配了 ADMIN_TOKEN 时，业务写类端点仍处于未配置（503）状态
        os.environ.pop(ENV_TOKEN, None)
        os.environ["ADMIN_TOKEN"] = "admin-only"
        self.assertEqual(configured_token(), "")
        d = authorize("/api/play/text", "POST", authorization="Bearer admin-only",
                      expected_token=configured_token())
        self.assertEqual(d.status, 503)

    def test_custom_environ_mapping(self):
        self.assertEqual(configured_token({ENV_TOKEN: "xyz"}), "xyz")
        self.assertEqual(configured_token({}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
