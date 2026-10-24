"""业务 API 鉴权纯函数测试（ticket 06；多 token 集合见 ticket 12 / notes/10 §2）。

不启动 aiohttp、不连音箱、不加载任何重依赖：只测 `core/services/auth.py` 的纯逻辑。

覆盖：
  1. 路径分类：health / read / write（含「未登记即按写类失败关闭」与敏感读）
  2. Bearer 头解析：缺失、空、错误 scheme、空凭据、大小写、多余空白
  3. 判定：正确 token 放行（带 label）；缺失 401；错误 403；未配置 503；
     配置非法 503（即使带了正确 token 也拒绝——半残配置必然被发现）
  4. TokenSet 解析：格式、label/token 字符集、label 重复、token 值重复、空条目
  5. configured_tokens 组装：仅 legacy、仅集合、并存合并、冲突判非法
  6. 管理面板 token（ADMIN_TOKEN）与业务 token 分离，互不回退
  7. 错误文案不含 token 明文，也不含服务端内部路径

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
    ENV_TOKENS,
    KIND_HEALTH,
    KIND_READ,
    KIND_WRITE,
    LEGACY_LABEL,
    TokenSet,
    authorize,
    classify_route,
    configured_tokens,
    parse_bearer,
    parse_token_set,
)

TOKEN = "s3cr3t-business-token"   # 旧规则下的值（不含冒号即可）；仅 legacy 时不校验字符集

# 新规则下的合法 token：[A-Za-z0-9._~-]{32,128}
T1 = "a" * 32
T2 = "b" * 33
T3 = "c" * 40
BAD_SHORT = "short-token"


def token_set(*pairs: tuple[str, str]) -> TokenSet:
    return TokenSet(entries=tuple(pairs))


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


class ParseTokenSetTest(unittest.TestCase):
    """`BRIDGE_API_TOKENS` 的解析与校验（ticket 12）。"""

    def test_valid_entries(self):
        ts = parse_token_set(f"ha-tts:{T1}, wecom:{T2}")
        self.assertTrue(ts.valid)
        self.assertEqual(ts.labels, ("ha-tts", "wecom"))
        self.assertEqual(ts.entries, (("ha-tts", T1), ("wecom", T2)))

    def test_blank_raw_is_empty_and_valid(self):
        for raw in ("", "   "):
            ts = parse_token_set(raw)
            self.assertTrue(ts.valid)
            self.assertEqual(ts.entries, ())

    def test_empty_segment_is_invalid(self):
        ts = parse_token_set(f"ha-tts:{T1},,ops:{T2}")
        self.assertFalse(ts.valid)
        self.assertTrue(any("空条目" in e for e in ts.errors))
        # 整份非法时错误信息不得含 token 值
        for value in (T1, T2):
            for e in ts.errors:
                self.assertNotIn(value, e)

    def test_missing_colon_is_invalid(self):
        # 无冒号的条目（label 与 token 粘在一起）→ 非法
        ts = parse_token_set(f"ha-tts{T1}")
        self.assertFalse(ts.valid)
        self.assertTrue(any("分隔符" in e for e in ts.errors))

    def test_bad_label_rejected(self):
        for label in ("HA", "ha_tts", "", "a" * 33):
            ts = parse_token_set(f"{label}:{T1}")
            self.assertFalse(ts.valid, label)
            self.assertTrue(any("label" in e for e in ts.errors), label)

    def test_bad_token_rejected(self):
        for token in (BAD_SHORT, "!" * 40, "", "a" * 129):
            ts = parse_token_set(f"ha-tts:{token}")
            self.assertFalse(ts.valid, token[:10])
            self.assertTrue(any("token" in e for e in ts.errors), token[:10])

    def test_duplicate_label_is_invalid(self):
        ts = parse_token_set(f"ha-tts:{T1},ha-tts:{T2}")
        self.assertFalse(ts.valid)
        self.assertTrue(any("label 重复" in e for e in ts.errors))

    def test_duplicate_token_value_is_invalid(self):
        ts = parse_token_set(f"ha-tts:{T1},wecom:{T1}")
        self.assertFalse(ts.valid)
        self.assertTrue(any("token 值" in e for e in ts.errors))
        # 错误信息只点名 label，不回显值
        for e in ts.errors:
            self.assertNotIn(T1, e)

    def test_colon_in_token_part_rejected(self):
        # token 按第一个冒号切分，剩余部分属 token，字符集不含冒号 → 非法
        ts = parse_token_set(f"ha-tts:{T1}:extra")
        self.assertFalse(ts.valid)


class AuthorizeTest(unittest.TestCase):
    """判定函数：这是中间件的唯一决策点。"""

    def test_health_and_read_always_allowed(self):
        for path in ("/api/health", "/api/status", "/api/inject", "/api/inject/relay"):
            d = authorize(path, "GET", authorization=None, tokens=TokenSet())
            self.assertTrue(d.allowed, path)
            self.assertEqual(d.status, 200, path)
            # 未配置 token 也要能读；带错 token 也不影响只读端点
            d = authorize(
                path, "GET", authorization="Bearer wrong",
                tokens=token_set(("ha-tts", T1)),
            )
            self.assertTrue(d.allowed, path)

    def test_write_allowed_with_correct_token_and_label(self):
        d = authorize(
            "/api/play/text",
            "POST",
            authorization=f"Bearer {T1}",
            tokens=token_set(("ha-tts", T1), ("wecom", T2)),
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.status, 200)
        self.assertEqual(d.code, "ok")
        self.assertEqual(d.label, "ha-tts")

    def test_label_matches_the_entry_that_hit(self):
        for label, token in (("ha-tts", T1), ("wecom", T2), ("ops", T3)):
            d = authorize(
                "/api/wakeup", "POST", authorization=f"Bearer {token}",
                tokens=token_set(("ha-tts", T1), ("wecom", T2), ("ops", T3)),
            )
            self.assertTrue(d.allowed, label)
            self.assertEqual(d.label, label)

    def test_legacy_only_behaves_like_single_token(self):
        d = authorize(
            "/api/play/text", "POST", authorization=f"Bearer {TOKEN}",
            tokens=token_set((LEGACY_LABEL, TOKEN)),
        )
        self.assertTrue(d.allowed)
        self.assertEqual(d.label, LEGACY_LABEL)

    def test_write_without_header_is_401(self):
        d = authorize(
            "/api/play/text", "POST", authorization=None,
            tokens=token_set(("ha-tts", T1)),
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 401)
        self.assertEqual(d.code, "token_missing")
        self.assertEqual(d.label, "")
        # 401 应提示客户端用 Bearer
        self.assertIn(("WWW-Authenticate", "Bearer"), d.headers)

    def test_write_with_wrong_scheme_is_401(self):
        for header in ("Basic " + T1, T1, "Bearer", "Bearer   "):
            d = authorize(
                "/api/wakeup", "POST", authorization=header,
                tokens=token_set(("ha-tts", T1)),
            )
            self.assertFalse(d.allowed, header)
            self.assertEqual(d.status, 401, header)
            self.assertEqual(d.code, "token_missing", header)

    def test_write_with_wrong_token_is_403(self):
        d = authorize(
            "/api/interrupt", "POST", authorization="Bearer not-the-token",
            tokens=token_set(("ha-tts", T1)),
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 403)
        self.assertEqual(d.code, "token_invalid")
        self.assertEqual(d.label, "")
        self.assertEqual(d.headers, ())

    def test_unconfigured_write_is_503_with_hint(self):
        for path, method in (
            ("/api/play/text", "POST"),
            ("/api/wakeup", "POST"),
            ("/api/audio_input", "POST"),
            ("/api/inject", "POST"),
            ("/api/inject/stream", "POST"),
        ):
            d = authorize(path, method, authorization=None, tokens=TokenSet())
            self.assertFalse(d.allowed, path)
            self.assertEqual(d.status, 503, path)
            self.assertEqual(d.code, "auth_unconfigured", path)
            # 必须给出「如何配置」，且只给变量名
            self.assertIn(ENV_TOKENS, d.error, path)
            self.assertIn("Authorization: Bearer", d.error, path)

    def test_invalid_config_is_503_even_with_correct_token(self):
        # 半残配置必须整份失败关闭：带正确 token 也不能放行（否则是静默错觉）
        d = authorize(
            "/api/play/text", "POST", authorization=f"Bearer {T1}",
            tokens=TokenSet(entries=(("ha-tts", T1),), errors=("条目 2: 空条目",)),
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 503)
        self.assertEqual(d.code, "auth_config_invalid")
        # 错误文案不给条目级细节（细节只在服务端日志），也不含值
        self.assertNotIn(T1, d.error)

    def test_non_ascii_token_does_not_crash(self):
        # hmac.compare_digest 对非 ASCII str 会抛 TypeError —— 必须按 bytes 比较
        d = authorize(
            "/api/play/text", "POST", authorization="Bearer 口令",
            tokens=token_set(("ha-tts", T1)),
        )
        self.assertFalse(d.allowed)
        self.assertEqual(d.status, 403)

    def test_errors_do_not_leak_token_or_paths(self):
        decisions = [
            authorize("/api/play/text", "POST", authorization=None,
                      tokens=token_set(("ha-tts", T1))),
            authorize("/api/play/text", "POST", authorization="Bearer wrong",
                      tokens=token_set(("ha-tts", T1))),
            authorize("/api/play/text", "POST", authorization=None, tokens=TokenSet()),
            authorize("/api/play/text", "POST", authorization=f"Bearer {T1}",
                      tokens=TokenSet(entries=(("ha-tts", T1),), errors=("x",))),
        ]
        for d in decisions:
            self.assertNotIn(T1, d.error)
            self.assertNotIn("wrong", d.error)
            for leak in ("/app", "/mnt", "C:\\", "/data"):
                self.assertNotIn(leak, d.error, (d.code, leak))


class ConfiguredTokensTest(unittest.TestCase):
    """环境变量组装：ENV_TOKENS 主、ENV_TOKEN 作 legacy 并存。"""

    def setUp(self):
        self._saved = {
            ENV_TOKEN: os.environ.get(ENV_TOKEN),
            ENV_TOKENS: os.environ.get(ENV_TOKENS),
            "ADMIN_TOKEN": os.environ.get("ADMIN_TOKEN"),
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_unset_or_blank_is_empty(self):
        self.assertEqual(configured_tokens({}), TokenSet())
        self.assertEqual(configured_tokens({ENV_TOKEN: "   "}), TokenSet())
        self.assertEqual(configured_tokens({ENV_TOKENS: "  "}), TokenSet())

    def test_legacy_only_keeps_old_behavior(self):
        # 只配 BRIDGE_API_TOKEN：单一 legacy entry，不做字符集校验（旧值兼容）
        ts = configured_tokens({ENV_TOKEN: "  abc  "})
        self.assertTrue(ts.valid)
        self.assertEqual(ts.entries, ((LEGACY_LABEL, "abc"),))
        # 旧规则下的短值/特殊字符值仍然有效
        ts = configured_tokens({ENV_TOKEN: TOKEN})
        self.assertTrue(ts.valid)
        self.assertEqual(ts.entries, ((LEGACY_LABEL, TOKEN),))

    def test_tokens_only(self):
        ts = configured_tokens({ENV_TOKENS: f"ha-tts:{T1},wecom:{T2}"})
        self.assertTrue(ts.valid)
        self.assertEqual(ts.labels, ("ha-tts", "wecom"))

    def test_tokens_merged_with_legacy(self):
        ts = configured_tokens({
            ENV_TOKENS: f"ha-tts:{T1}",
            ENV_TOKEN: TOKEN,
        })
        self.assertTrue(ts.valid)
        self.assertEqual(ts.labels, ("ha-tts", LEGACY_LABEL))

    def test_invalid_tokens_config_stays_invalid(self):
        ts = configured_tokens({
            ENV_TOKENS: f"ha-tts:{BAD_SHORT}",
            ENV_TOKEN: TOKEN,
        })
        self.assertFalse(ts.valid)
        self.assertTrue(any("token" in e for e in ts.errors))

    def test_legacy_label_conflict_is_invalid(self):
        ts = configured_tokens({
            ENV_TOKENS: f"{LEGACY_LABEL}:{T1}",
            ENV_TOKEN: TOKEN,
        })
        self.assertFalse(ts.valid)
        self.assertTrue(any(LEGACY_LABEL in e for e in ts.errors))

    def test_legacy_value_duplicate_is_invalid(self):
        ts = configured_tokens({
            ENV_TOKENS: f"ha-tts:{TOKEN}-aaaaaaaaaaaaaaaaaaaaaa",
            ENV_TOKEN: TOKEN,
        })
        # TOKEN（21 字符）是上面那条 token 的前缀但不是同值 ⇒ 合法；
        # 只有完全相同的值才判重复
        self.assertTrue(ts.valid)
        ts = configured_tokens({
            ENV_TOKENS: f"ha-tts:{'x' * 32}",
            ENV_TOKEN: "x" * 32,
        })
        self.assertFalse(ts.valid)
        self.assertTrue(any("label=ha-tts" in e for e in ts.errors))
        for e in ts.errors:
            self.assertNotIn("x" * 32, e)  # 不回显值

    def test_admin_token_is_not_used(self):
        # token 分离：只配了 ADMIN_TOKEN 时，业务写类端点仍处于未配置（503）状态
        d = authorize(
            "/api/play/text", "POST", authorization="Bearer admin-only",
            tokens=configured_tokens({"ADMIN_TOKEN": "admin-only"}),
        )
        self.assertEqual(d.status, 503)

    def test_reads_and_strips_from_environ(self):
        self.assertEqual(
            configured_tokens({ENV_TOKENS: f"  ha-tts :  {T1}  "}).labels,
            ("ha-tts",),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
