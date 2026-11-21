"""cch_cast 凭据/通道契约（2026-11-21 事故回归）：缺 BRIDGE_TOKEN 必须显式失败。

事故：部署漏 BRIDGE_TOKEN → musicctl/speak 在客户端前置失败、host_cmd 报 NEED_M5，
外部只看到「设备未就绪」。两条硬约束：
1. 空 token 不得派生出可公开计算的 hostcmd key（fail closed）；
2. host_cmd 在缺 token 时返回 NO_TOKEN（可诊断），不发广播。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from core.music import cch_cast


class TokenContractTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("BRIDGE_TOKEN", None)
        cch_cast._token_warned = False

    def tearDown(self):
        if self._env is not None:
            os.environ["BRIDGE_TOKEN"] = self._env

    def test_missing_token_yields_empty_key(self):
        with mock.patch.object(cch_cast, "_cfg", lambda k, d=None: ""):
            self.assertEqual("", cch_cast.host_derived_key())

    def test_token_from_env_derives_stable_key(self):
        os.environ["BRIDGE_TOKEN"] = "test-token"
        with mock.patch.object(cch_cast, "_cfg", lambda k, d=None: ""):
            k1 = cch_cast.host_derived_key()
            k2 = cch_cast.host_derived_key()
        self.assertEqual(64, len(k1))
        self.assertEqual(k1, k2)
        self.assertNotEqual("", k1)


class HostCmdNoTokenTest(unittest.IsolatedAsyncioTestCase):
    async def test_host_cmd_refuses_without_token(self):
        calls = []

        async def fake_musicctl(op, body=None):
            calls.append(op)
            return True, {}

        with mock.patch.object(cch_cast, "host_derived_key", lambda: ""), \
                mock.patch.object(cch_cast, "musicctl", fake_musicctl):
            ok, status = await cch_cast.host_cmd("play", url="http://x/1.mp3")
        self.assertFalse(ok)
        self.assertEqual("NO_TOKEN", status)
        self.assertEqual([], calls, "缺 token 时不得向 9102 发任何 hostcmd")

    async def test_host_cmd_maps_token_error_to_no_token(self):
        async def fake_musicctl(op, body=None):
            return False, {"error": "tts token not configured"}

        with mock.patch.object(cch_cast, "host_derived_key", lambda: "deadbeef"), \
                mock.patch.object(cch_cast, "musicctl", fake_musicctl):
            ok, status = await cch_cast.host_cmd("play", url="http://x/1.mp3")
        self.assertFalse(ok)
        self.assertEqual("NO_TOKEN", status)


if __name__ == "__main__":
    unittest.main()
