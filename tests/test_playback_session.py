# -*- coding: utf-8 -*-
"""PlaybackSession 状态机守门（ADR-0001 L15 / 架构收敛不变量）。"""
import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from core.music import cch_cast, playback_observation, playback_session
from core.tools import song_request as sr


def _prepared():
    return playback_session.PreparedPlayback(
        play_url="http://ticket/x",
        item={"id": "NEW", "name": "风吹麦浪", "source": "kugou"},
        items=[{"id": "NEW", "name": "风吹麦浪", "source": "kugou"}],
        meta={}, bg_url="", intent="single")


class PlaybackSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._state_backup = dict(sr._cch_state)
        self.session = playback_session.PlaybackSession("cch-test")
        # 每个测试独立实例，隔离全局 registry
        playback_session._SESSIONS["cch-test"] = self.session

    def tearDown(self):
        sr._cch_state.clear()
        sr._cch_state.update(self._state_backup)
        playback_session._SESSIONS.pop("cch-test", None)

    async def test_silence_then_prepare_then_play(self):
        """状态机：IDLE→SILENCED→PREPARING→PLAYING，stop 只一次。"""
        cmds = []

        async def fake_host_cmd(cmd, **kw):
            cmds.append(cmd)
            return True, "OK"

        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd", fake_host_cmd))
            stack.enter_context(patch.object(playback_observation, "observe_play",
                                             AsyncMock(return_value=True)))
            stack.enter_context(patch.object(cch_cast, "build_render_data",
                                             MagicMock(return_value={})))
            stack.enter_context(patch.object(cch_cast, "inject_lyric",
                                             MagicMock(side_effect=lambda rd, sid: dict(rd))))
            render = stack.enter_context(patch.object(cch_cast, "render_display",
                                                      AsyncMock(return_value=True)))
            stack.enter_context(patch.object(cch_cast, "write_lyric",
                                             AsyncMock(return_value=True)))
            stack.enter_context(patch.object(cch_cast, "push_queue", AsyncMock()))
            stack.enter_context(patch("core.music.cch_history.note", MagicMock()))
            await self.session.silence()
            self.assertEqual(self.session.phase, playback_session.PlaybackPhase.SILENCED)
            self.session.prepare()
            self.assertEqual(self.session.phase, playback_session.PlaybackPhase.PREPARING)
            ok = await self.session.play(_prepared())

        self.assertTrue(ok)
        self.assertEqual(self.session.phase, playback_session.PlaybackPhase.PLAYING)
        # 用户裁决：唤醒词静默 = pause（audioStatus:9，实例保留），保持到 bridge 新指令；
        # 不用 stop（audioStatus:8，杀实例导致位置丢失/重播竞速）
        self.assertEqual(["pause", "play"], cmds, "silence 用 pause 保持实例，bridge 备好再 play")
        self.assertEqual(1, render.await_count, "renderDisplay 恰好一次")

    async def test_report_suppressed_during_preparing(self):
        """PREPARING 期间所有恢复事件 no-op——一个状态判据。"""
        with ExitStack() as stack:
            replay = stack.enter_context(patch(
                "core.music.cch_np.replay_current", AsyncMock(return_value={"action": "replayed"})))
            await self.session.silence()
            self.session.prepare()
            for kind in ("freeze", "toggle", "tts_resume", "track_finish"):
                result = await self.session.report(kind)
                self.assertEqual("suppressed", result["action"])

        replay.assert_not_awaited()

    async def test_freeze_recovers_back_to_playing(self):
        """freeze 恢复成功后必须回到 PLAYING，否则后续换代被 controlled() 吞掉。"""
        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd",
                                             AsyncMock(return_value=(True, "OK"))))
            stack.enter_context(patch("core.music.cch_np.replay_current",
                                      AsyncMock(return_value={"action": "replayed"})))
            await self.session.silence()
            self.session.prepare()
            self.session.phase = playback_session.PlaybackPhase.PLAYING
            result = await self.session.report("freeze")

        self.assertEqual("replayed", result["action"])
        self.assertEqual(playback_session.PlaybackPhase.PLAYING, self.session.phase,
                         "恢复成功后必须可继续接受换代事件")
        self.assertFalse(self.session.controlled())

    async def test_observation_window_covers_worker_poll_cycle(self):
        """观测窗必须 >= worker 一个 poll 周期（15s）+ 余量——STATE 上报天然滞后。"""
        import inspect
        from core.music import playback_observation as obs

        sig = inspect.signature(obs.observe_play)
        default = sig.parameters["timeout_s"].default
        self.assertGreaterEqual(default, 15.0,
                                "worker poll=15s，3s 窗会把成功起播误判为失败")

    async def test_play_uses_fresh_observation_window(self):
        """观测必须带 since_ts：上一首遗留的 play 不得确认本次起播。"""
        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd",
                                             AsyncMock(return_value=(True, "OK"))))
            obs_mock = stack.enter_context(patch.object(
                playback_observation, "observe_play",
                AsyncMock(return_value=True)))
            stack.enter_context(patch.object(cch_cast, "build_render_data",
                                             MagicMock(return_value={})))
            stack.enter_context(patch.object(cch_cast, "inject_lyric",
                                             MagicMock(side_effect=lambda rd, sid: dict(rd))))
            stack.enter_context(patch.object(cch_cast, "render_display", AsyncMock()))
            stack.enter_context(patch.object(cch_cast, "write_lyric", AsyncMock()))
            stack.enter_context(patch.object(cch_cast, "push_queue", AsyncMock()))
            stack.enter_context(patch("core.music.cch_history.note", MagicMock()))
            await self.session.silence()
            self.session.prepare()
            await self.session.play(_prepared())

        self.assertIsNotNone(obs_mock.call_args.kwargs.get("since_ts"),
                             "观测须带静音起点，排除上一首遗留 play")

    async def test_silence_falls_back_to_stop_when_pause_unavailable(self):
        """实例已死（pause 返回 NEED_M5）时回退 stop 清场，不留旧曲残影。"""
        cmds = []

        async def fake_host_cmd(cmd, **kw):
            cmds.append(cmd)
            return (False, "NEED_M5") if cmd == "pause" else (True, "OK")

        with ExitStack() as stack:
            stack.enter_context(patch.object(cch_cast, "host_cmd", fake_host_cmd))
            await self.session.silence()

        self.assertEqual(["pause", "stop"], cmds,
                         "pause 不可用（死实例）时补一个 stop 清场")

    async def test_new_intent_cancels_inflight_recovery(self):
        """新点播到达必须作废旧曲的在途恢复任务（真机 FREEZE 取票 30s
        跨越新点播窗口，最终把旧曲 play 发出来）。"""
        import asyncio

        started = asyncio.Event()
        finished = []

        async def slow_replay():
            started.set()
            try:
                await asyncio.sleep(10)  # 模拟取票/换代长任务
                finished.append("old-play")
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise

        with ExitStack() as stack:
            stack.enter_context(patch("core.music.cch_np.replay_current", slow_replay))
            await self.session.silence()
            self.session.phase = playback_session.PlaybackPhase.PLAYING
            task = self.session.dispatch("freeze")
            await started.wait()
            # 新意图到达：silence 必须取消在途恢复
            await self.session.silence()
            await asyncio.sleep(0)
            try:
                await task
            except asyncio.CancelledError:
                pass

        self.assertIn("cancelled", finished, "在途旧曲恢复必须被新意图取消")
        self.assertNotIn("old-play", finished)

    async def test_two_sessions_isolated(self):
        """per-renderer 隔离（ADR L15）：一端事务不影响另一端。"""
        other = playback_session.PlaybackSession("lx06")
        await self.session.silence()  # cch-test 进入 SILENCED
        self.assertFalse(other.controlled(), "另一 renderer 不应被牵连")
        self.assertTrue(self.session.controlled())

    async def test_play_idempotent_outside_preparing(self):
        """非 PREPARING 态 play 直接拒绝（防止重复起播）。"""
        ok = await self.session.play(_prepared())
        self.assertFalse(ok)
        self.assertEqual(self.session.phase, playback_session.PlaybackPhase.IDLE)


if __name__ == "__main__":
    unittest.main()
