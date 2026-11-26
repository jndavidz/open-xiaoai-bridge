# -*- coding: utf-8 -*-
"""host_cmd 重试语义守门（2026-11-26 真机重复起播根因）。

真机：host_cmd("play") 回执 TIMEOUT 后内部自动重发一次 ⇒ 设备反复起播
（pid 7342143 → 7342144）。play 的真相由 observe_play 裁决，不应靠回执重发保证送达。
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from core.music import cch_cast


class HostCmdRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_play_skips_retry_even_on_timeout(self):
        """retry_once=False 时回执超时也不重发——只调用一次 musicctl hostcmd。"""
        calls = []

        async def fake_musicctl(op, payload=None):
            calls.append(op)
            if op == "hostcmd":
                return True, {}
            return True, {"result": ""}  # 永不返回匹配 cmd_id 的 OK → 触发超时路径

        with patch.object(cch_cast, "host_derived_key", return_value="tok"), \
             patch.object(cch_cast, "musicctl", fake_musicctl):
            ok, status = await cch_cast.host_cmd(
                "play", url="http://x", timeout_s=0.1, retry_once=False)

        self.assertFalse(ok)
        self.assertEqual("TIMEOUT", status)
        self.assertEqual(1, calls.count("hostcmd"),
                         "retry_once=False 时 play 不得重发（否则设备重复起播）")

    async def test_default_retry_still_resends(self):
        """默认（其它命令）保留重发——广播间歇丢失兜底，不破坏既有语义。"""
        calls = []

        async def fake_musicctl(op, payload=None):
            calls.append(op)
            if op == "hostcmd":
                return True, {}
            return True, {"result": ""}

        with patch.object(cch_cast, "host_derived_key", return_value="tok"), \
             patch.object(cch_cast, "musicctl", fake_musicctl):
            await cch_cast.host_cmd("stop", timeout_s=0.1)

        self.assertEqual(2, calls.count("hostcmd"), "默认保留一次重发（广播丢包兜底）")


if __name__ == "__main__":
    unittest.main()
