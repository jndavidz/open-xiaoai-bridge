# -*- coding: utf-8 -*-
"""点播提交点的架构不变量守门（2026-11-26 真机三 symptom 同源修复）。"""
import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from core.music import cch_cast, playback_commit, playback_observation
from core.tools import song_request as sr


class PlaybackCommitTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._state_backup = dict(sr._cch_state)

    def tearDown(self):
        sr._cch_state.clear()
        sr._cch_state.update(self._state_backup)

    def _kwargs(self, items=None):
        items = items or [{"id": "NEW", "name": "风吹麦浪", "source": "kugou"}]
        return dict(play_url="http://ticket/x", item=items[0], items=items,
                    meta={}, bg_url="", intent="single")

    async def test_receipt_timeout_but_observed_play_commits(self):
        """不变量 1：回执 TIMEOUT 但设备已起播仍提交（真机主症状）。"""
        calls = {"render": []}

        async def fake_render(rd):
            calls["render"].append(rd)
            return True

        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd",
                                             AsyncMock(return_value=(False, "TIMEOUT"))))
            stack.enter_context(patch.object(cch_cast, "host_cmd_nowait", AsyncMock()))
            stack.enter_context(patch.object(playback_observation, "observe_play",
                                             AsyncMock(return_value=True)))
            stack.enter_context(patch.object(cch_cast, "build_render_data",
                                             MagicMock(return_value={})))
            stack.enter_context(patch.object(cch_cast, "render_display", fake_render))
            stack.enter_context(patch.object(cch_cast, "attach_lyric",
                                             AsyncMock(return_value={})))
            stack.enter_context(patch.object(cch_cast, "push_queue", AsyncMock()))
            stack.enter_context(patch("core.music.cch_history.note", MagicMock()))
            sr._cch_state.update({"entry": 1, "items": [{"id": "OLD"}], "played": [1]})
            result = await playback_commit.commit_playback(**self._kwargs())
            await asyncio.sleep(0)  # 让 presentation task 起步

        self.assertTrue(result.ok, "观测确认即算成功，不受回执 TIMEOUT 影响")
        self.assertEqual("NEW", sr._cch_state["items"][0]["id"])
        self.assertEqual(0, sr._cch_state["entry"])
        self.assertEqual([], sr._cch_state["played"])
        self.assertGreaterEqual(len(calls["render"]), 1)

    async def test_no_observation_and_bad_receipt_fails_without_state_change(self):
        """不变量 2：双失败则整笔不提交（fail-closed，不留半状态）。"""
        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd",
                                             AsyncMock(return_value=(False, "TIMEOUT"))))
            stack.enter_context(patch.object(cch_cast, "host_cmd_nowait", AsyncMock()))
            stack.enter_context(patch.object(playback_observation, "observe_play",
                                             AsyncMock(return_value=False)))
            sr._cch_state.update({"entry": 3, "items": [{"id": "OLD"}]})
            result = await playback_commit.commit_playback(**self._kwargs())

        self.assertFalse(result.ok)
        self.assertEqual("OLD", sr._cch_state["items"][0]["id"], "失败不得改所有权")
        self.assertEqual(3, sr._cch_state["entry"])

    async def test_presentation_survives_caller_cancellation(self):
        """不变量 3：LLM tool 取消后呈现任务仍完成（UI/歌词不落旧曲）。"""
        done = []

        async def slow_attach(item, rd):
            await asyncio.sleep(0.05)
            done.append("lyric")
            return rd

        async def fake_render(rd):
            done.append("render")
            return True

        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd",
                                             AsyncMock(return_value=(True, "OK"))))
            stack.enter_context(patch.object(cch_cast, "host_cmd_nowait", AsyncMock()))
            stack.enter_context(patch.object(playback_observation, "observe_play",
                                             AsyncMock(return_value=True)))
            stack.enter_context(patch.object(cch_cast, "build_render_data",
                                             MagicMock(return_value={})))
            stack.enter_context(patch.object(cch_cast, "render_display", fake_render))
            stack.enter_context(patch.object(cch_cast, "attach_lyric", slow_attach))
            stack.enter_context(patch.object(cch_cast, "push_queue", AsyncMock()))
            stack.enter_context(patch("core.music.cch_history.note", MagicMock()))
            task = asyncio.create_task(
                playback_commit.commit_playback(**self._kwargs()))
            await asyncio.sleep(0)
            task.cancel()  # 模拟 LLM tool 超时取消调用方
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.sleep(0.12)  # 呈现 task 是独立任务，应跑完

        self.assertIn("render", done, "取消调用方后显示补投仍须发生")
        self.assertIn("lyric", done, "取消调用方后歌词仍须补投")


if __name__ == "__main__":
    unittest.main()
