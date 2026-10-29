"""TTS focus 的生产接线（B-07，会话级）。

用现有 announce / 对话夹具，focus 桥打桩——不连设备、不联网。覆盖：

  1. 会话级持有：hold 只暂停一次；释放归零后**去抖**恢复；新 hold 取消待恢复；
     force_release 立即放弃且不自动恢复。
  2. `speaker.announce`：播报持有/归还一次；暂停确认失败 ⇒ 拒绝播报。
  3. 连续对话：`start()` 进入即持有，结束时归还（异常同样归还）。
  4. 唤醒打断：托管模式不得直接停设备音频。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.music import focus_bridge  # noqa: E402
from core.music.tts_focus import FocusDenied, FocusToken  # noqa: E402

import test_announce  # noqa: E402
import test_pipeline  # noqa: E402


def _token(name: str = "f-wire") -> FocusToken:
    return FocusToken(name, "ms-1", 1, "e-1", 12.0, "announce", 0.0)


class HoldTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        focus_bridge._reset_for_tests()

    async def test_hold_off_does_nothing(self) -> None:
        with mock.patch.object(focus_bridge, "enabled", return_value=False), \
                mock.patch.object(focus_bridge, "acquire", mock.AsyncMock()) as acquire:
            self.assertFalse(await focus_bridge.hold())
        acquire.assert_not_awaited()

    async def test_hold_pauses_once_and_debounces_release(self) -> None:
        token = _token()
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
                mock.patch.object(focus_bridge, "acquire",
                                  mock.AsyncMock(return_value=token)) as acquire, \
                mock.patch.object(focus_bridge, "release",
                                  mock.AsyncMock(return_value=(True, "resumed"))) as release:
            self.assertTrue(await focus_bridge.hold())
            self.assertTrue(await focus_bridge.hold())      # 引用计数 2
            await focus_bridge.release_hold(grace_s=0.01)
            await asyncio.sleep(0.03)
            release.assert_not_awaited()                   # 还有一层持有
            await focus_bridge.release_hold(grace_s=0.01)
            await asyncio.sleep(0.05)
        self.assertEqual(1, acquire.await_count, "整段会话只暂停一次")
        self.assertEqual(1, release.await_count, "归零后去抖恢复一次")

    async def test_new_hold_cancels_a_pending_release(self) -> None:
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
                mock.patch.object(focus_bridge, "acquire",
                                  mock.AsyncMock(return_value=_token())), \
                mock.patch.object(focus_bridge, "release",
                                  mock.AsyncMock(return_value=(True, "resumed"))) as release:
            await focus_bridge.hold()
            await focus_bridge.release_hold(grace_s=0.05)
            await focus_bridge.hold()      # 开场白结束→对话开始的缝隙
            await asyncio.sleep(0.1)
        release.assert_not_awaited()

    async def test_force_release_drops_without_resuming(self) -> None:
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
                mock.patch.object(focus_bridge, "acquire",
                                  mock.AsyncMock(return_value=_token())), \
                mock.patch.object(focus_bridge, "release",
                                  mock.AsyncMock()) as release:
            await focus_bridge.hold()
            await focus_bridge.force_release()
            await asyncio.sleep(0.02)
        release.assert_not_awaited()


class AnnounceFocusTest(test_announce.AnnounceTestBase):
    def test_announce_holds_and_returns_focus(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", test_announce._context(0))

        async def scenario():
            async with self.speaker.announce(text="客厅音响已就绪", reason="t"):
                await self.speaker.play(text="客厅音响已就绪", blocking=False)

        with mock.patch("core.music.focus_bridge.hold",
                        mock.AsyncMock(return_value=True)) as hold, \
                mock.patch("core.music.focus_bridge.release_hold",
                           mock.AsyncMock()) as release:
            test_announce._run(scenario())
        hold.assert_awaited_once()
        release.assert_awaited_once()

    def test_focus_released_even_when_body_raises(self) -> None:
        self.device.on("player_get_context", test_announce._context(0))

        async def scenario():
            async with self.speaker.announce(text="你好", reason="t"):
                raise RuntimeError("body boom")

        with mock.patch("core.music.focus_bridge.hold",
                        mock.AsyncMock(return_value=True)), \
                mock.patch("core.music.focus_bridge.release_hold",
                           mock.AsyncMock()) as release:
            with self.assertRaises(RuntimeError):
                test_announce._run(scenario())
        release.assert_awaited_once()

    def test_focus_denied_refuses_the_announcement(self) -> None:
        self.device.on("player_get_context", test_announce._context(0))

        async def scenario():
            async with self.speaker.announce(text="你好", reason="t"):
                pass

        with mock.patch("core.music.focus_bridge.hold",
                        mock.AsyncMock(side_effect=FocusDenied("pause not confirmed",
                                                               code="pause_unconfirmed"))):
            with self.assertRaises(test_announce.speaker_module.AnnounceRejected):
                test_announce._run(scenario())


class ConversationFocusTest(test_pipeline.ConversationLoopTestBase):
    async def test_conversation_holds_focus_for_the_whole_session(self) -> None:
        with mock.patch("core.music.focus_bridge.hold",
                        mock.AsyncMock(return_value=True)) as hold, \
                mock.patch("core.music.focus_bridge.release_hold",
                           mock.AsyncMock()) as release, \
                mock.patch.object(type(self.controller), "_conversation_loop",
                                  mock.AsyncMock(return_value=None)):
            await self.controller.start()
        hold.assert_awaited_once()
        release.assert_awaited_once()

    async def test_conversation_releases_focus_on_error(self) -> None:
        with mock.patch("core.music.focus_bridge.hold",
                        mock.AsyncMock(return_value=True)), \
                mock.patch("core.music.focus_bridge.release_hold",
                           mock.AsyncMock()) as release, \
                mock.patch.object(type(self.controller), "_conversation_loop",
                                  mock.AsyncMock(side_effect=RuntimeError("boom"))):
            await self.controller.start()
        release.assert_awaited_once()


class WakeupManagedAudioTest(unittest.IsolatedAsyncioTestCase):
    """唤醒打断：托管模式不得直接停设备音频（停了就回不来）。"""

    def _manager(self):
        from core import wakeup_session as ws

        return object.__new__(ws.WakeupSessionManager)

    async def test_managed_mode_keeps_audio_for_focus(self) -> None:
        manager = self._manager()
        stop = mock.AsyncMock()
        record = mock.AsyncMock()
        with mock.patch("core.music.focus_bridge.manages_music", return_value=True), \
                mock.patch.object(type(manager), "_stop_device_playback", stop), \
                mock.patch("core.xiaoai.XiaoAI.start_recording", record):
            await manager._interrupt_device_playback()
        stop.assert_not_awaited()
        record.assert_awaited_once()

    async def test_legacy_mode_falls_back_to_stop(self) -> None:
        manager = self._manager()
        stop = mock.AsyncMock()
        with mock.patch("core.music.focus_bridge.manages_music", return_value=False), \
                mock.patch.object(type(manager), "_stop_device_playback", stop), \
                mock.patch("core.tools.song_request.is_paused_for_wakeup", return_value=False), \
                mock.patch("core.tools.song_request.pause_for_wakeup",
                           mock.AsyncMock(return_value=False)):
            await manager._interrupt_device_playback()
        stop.assert_awaited_once()

    async def test_managed_legacy_pause_still_wins_when_flagged(self) -> None:
        manager = self._manager()
        stop = mock.AsyncMock()
        record = mock.AsyncMock()
        with mock.patch("core.music.focus_bridge.manages_music", return_value=False), \
                mock.patch.object(type(manager), "_stop_device_playback", stop), \
                mock.patch("core.tools.song_request.is_paused_for_wakeup", return_value=True), \
                mock.patch("core.xiaoai.XiaoAI.start_recording", record):
            await manager._interrupt_device_playback()
        stop.assert_not_awaited()
        record.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
