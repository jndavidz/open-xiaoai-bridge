# -*- coding: utf-8 -*-
"""每渲染端独立 focus（05d）：一台设备播报不得影响另一台的音乐。"""
import asyncio
import unittest
from unittest import mock

from core.music import focus_bridge
from core.music.tts_focus import FocusToken


def _token(name="f"):
    return FocusToken(name, "ms-1", 1, "e-1", 12.0, "announce", 0.0)


class PerRendererFocusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        focus_bridge._reset_for_tests()

    async def test_holds_are_isolated_per_renderer(self):
        """cch 持有不应让 lx06 的 focus_holding 变真（两台设备互不打架）。"""
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
             mock.patch.object(focus_bridge, "acquire",
                               mock.AsyncMock(return_value=_token())):
            self.assertTrue(await focus_bridge.hold("conversation", renderer="cch"))
            self.assertTrue(focus_bridge.focus_holding("cch"))
            self.assertFalse(focus_bridge.focus_holding("lx06"),
                             "cch 的持有不得牵连 lx06")

    async def test_release_is_per_renderer(self):
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
             mock.patch.object(focus_bridge, "acquire",
                               mock.AsyncMock(return_value=_token())), \
             mock.patch.object(focus_bridge, "release",
                               mock.AsyncMock(return_value=(True, "resumed"))):
            await focus_bridge.hold("conversation", renderer="cch")
            await focus_bridge.hold("announce", renderer="lx06")
            await focus_bridge.release_hold(renderer="cch", grace_s=0.01)
            await asyncio.sleep(0.03)
            self.assertFalse(focus_bridge.focus_holding("cch"))
            self.assertTrue(focus_bridge.focus_holding("lx06"),
                            "释放 cch 不得影响 lx06 的持有")

    async def test_cch_acquire_uses_pause_and_release_never_resumes(self):
        """CCH 契约（§2.79）：pause 按住，release 不 resume 旧曲（会竞速）。"""
        # 不 mock release：让它走真实 CCH 分支（不碰 song_bridge）；
        # 用 song_bridge.focus 的 mock 证明「真 TtsFocus.release 从未被调用」。
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
             mock.patch("core.music.cch_cast.host_cmd",
                        mock.AsyncMock(return_value=(True, "SENT"))) as hc, \
             mock.patch("core.music.song_bridge.focus",
                        mock.Mock(return_value=None)) as focus_fn:
            await focus_bridge.hold("conversation", renderer="cch")
            hc.assert_awaited()
            self.assertEqual("pause", hc.await_args.args[0])
            await focus_bridge.release_hold(renderer="cch", grace_s=0.01)
            await asyncio.sleep(0.03)
            focus_fn.assert_not_called(), "CCH 释放不得触碰 LX06 TtsFocus（即不 resume）"

    async def test_default_renderer_is_lx06_backward_compatible(self):
        """既有调用（无 renderer 参数）仍走 lx06 原行为。"""
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
             mock.patch.object(focus_bridge, "acquire",
                               mock.AsyncMock(return_value=_token())):
            await focus_bridge.hold()
            self.assertTrue(focus_bridge.focus_holding("lx06"))
            self.assertFalse(focus_bridge.focus_holding("cch"))
