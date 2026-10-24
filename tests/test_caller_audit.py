"""审计调用方标识测试（ticket 09：默认不信任 X-Forwarded-For）。

背景：生产是 host 网络直连、无反向代理，`X-Forwarded-For` / `X-Real-IP`
可被任意局域网客户端伪造；默认信任等于让「是谁让音箱说话」的审计失效。

覆盖：
  1. 默认（TRUST_PROXY 未配置/假值）只认 TCP peer，伪造头被忽略
  2. `TRUST_PROXY=1` 时才读 XFF（取**最右**一跳，防客户端伪造最左值）/ X-Real-IP
  3. peer 缺失 / 空头 / 空值 → "unknown"，不抛异常
  4. 多跳 XFF 只取第一段；头部值去空白

用法：
    python3 tests/test_caller_audit.py
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.services.caller import (  # noqa: E402
    ENV_TRUST_PROXY,
    resolve_caller_tag,
    trust_proxy_enabled,
)

XFF = {"X-Forwarded-For": "203.0.113.7, 198.51.100.9"}


class TrustProxyFlagTest(unittest.TestCase):
    def test_unset_is_false(self):
        self.assertFalse(trust_proxy_enabled({}))
        self.assertFalse(trust_proxy_enabled({ENV_TRUST_PROXY: ""}))
        self.assertFalse(trust_proxy_enabled({ENV_TRUST_PROXY: "0"}))
        self.assertFalse(trust_proxy_enabled({ENV_TRUST_PROXY: "false"}))
        self.assertFalse(trust_proxy_enabled({ENV_TRUST_PROXY: "no"}))
        # 拼错/任意字符串不得被当成真值
        self.assertFalse(trust_proxy_enabled({ENV_TRUST_PROXY: "yess"}))

    def test_truthy_values(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            self.assertTrue(trust_proxy_enabled({ENV_TRUST_PROXY: value}), value)


class CallerTagTest(unittest.TestCase):
    def test_peer_is_default(self):
        self.assertEqual(resolve_caller_tag("10.10.10.6"), "10.10.10.6")
        self.assertEqual(
            resolve_caller_tag("10.10.10.6", {}, trust_proxy=False), "10.10.10.6"
        )

    def test_xff_ignored_by_default(self):
        # 核心回归：无 TRUST_PROXY 时，客户端自带的 XFF 不得进入审计
        self.assertEqual(resolve_caller_tag("10.10.10.6", XFF), "10.10.10.6")
        self.assertEqual(
            resolve_caller_tag("10.10.10.6", {"X-Real-IP": "1.2.3.4"}),
            "10.10.10.6",
        )

    def test_xff_used_only_when_trusted(self):
        # 取最右跳：最左值是客户端可伪造的，不能作为审计身份
        self.assertEqual(
            resolve_caller_tag("10.10.10.2", XFF, trust_proxy=True), "198.51.100.9"
        )
        self.assertEqual(
            resolve_caller_tag("10.10.10.2", {"X-Real-IP": " 198.51.100.9 "},
                               trust_proxy=True),
            "198.51.100.9",
        )
        # XFF 优先于 X-Real-IP
        self.assertEqual(
            resolve_caller_tag(
                "10.10.10.2",
                {"X-Forwarded-For": "203.0.113.7", "X-Real-IP": "198.51.100.9"},
                trust_proxy=True,
            ),
            "203.0.113.7",
        )

    def test_trusted_but_header_missing_falls_back_to_peer(self):
        self.assertEqual(
            resolve_caller_tag("10.10.10.6", {}, trust_proxy=True), "10.10.10.6"
        )
        self.assertEqual(
            resolve_caller_tag("10.10.10.6", {"X-Forwarded-For": "  ,  "},
                               trust_proxy=True),
            "10.10.10.6",
        )

    def test_missing_peer_is_unknown(self):
        self.assertEqual(resolve_caller_tag(None), "unknown")
        self.assertEqual(resolve_caller_tag(""), "unknown")
        self.assertEqual(resolve_caller_tag("   ", XFF), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
