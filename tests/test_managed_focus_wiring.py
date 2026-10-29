"""TTS focus 的生产接线（B-07）：播报与对话回复都要「取得—释放」。

用现有 announce / 对话轮夹具，focus 桥整体打桩——不连设备、不联网。
覆盖：

  1. focus_scope：托管关 = 不取不释；托管开 = 正常/异常都释放；
     不需要 focus 的情形（音乐未在播）静默跳过；required 时拒绝传播。
  2. `speaker.announce`：取得 focus 后播报，finally 释放并记录结果；异常也释放。
  3. 对话回复段循环：取得 focus → 逐段播放 → 释放（异常同样释放）。
"""

from __future__ import annotations

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


class FocusScopeTest(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_off_does_nothing(self) -> None:
        with mock.patch.object(focus_bridge, "enabled", return_value=False), \
                mock.patch.object(focus_bridge, "acquire", mock.AsyncMock()) as acquire, \
                mock.patch.object(focus_bridge, "release", mock.AsyncMock()) as release:
            async with focus_bridge.focus_scope("reply") as token:
                self.assertIsNone(token)
        acquire.assert_not_awaited()
        release.assert_not_awaited()

    async def test_enabled_on_releases_on_normal_and_error_paths(self) -> None:
        token = _token()
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
                mock.patch.object(focus_bridge, "acquire",
                                  mock.AsyncMock(return_value=token)), \
                mock.patch.object(focus_bridge, "release",
                                  mock.AsyncMock(return_value=(True, "resumed"))) as release:
            async with focus_bridge.focus_scope("reply") as got:
                self.assertIs(token, got)
            with self.assertRaises(RuntimeError):
                async with focus_bridge.focus_scope("reply"):
                    raise RuntimeError("boom")
        self.assertEqual(2, release.await_count, "正常与异常都必须释放")

    async def test_not_playing_is_not_an_error_unless_required(self) -> None:
        denied = FocusDenied("music is not playing", code="not_playing")
        with mock.patch.object(focus_bridge, "enabled", return_value=True), \
                mock.patch.object(focus_bridge, "acquire", mock.AsyncMock(side_effect=denied)):
            async with focus_bridge.focus_scope("reply") as token:
                self.assertIsNone(token)
            with self.assertRaises(FocusDenied):
                async with focus_bridge.focus_scope("announce", required=True):
                    pass


class AnnounceFocusTest(test_announce.AnnounceTestBase):
    def test_announce_takes_and_releases_focus(self) -> None:
        self.device.on("text_to_speech", '{"code": 0}')
        self.device.on("player_get_context", test_announce._context(0))
        token = _token()

        async def scenario():
            async with self.speaker.announce(text="客厅音响已就绪", reason="t") as session:
                await self.speaker.play(text="客厅音响已就绪", blocking=False)
            return session

        with mock.patch("core.music.focus_bridge.enabled", return_value=True), \
                mock.patch("core.music.focus_bridge.acquire",
                           mock.AsyncMock(return_value=token)) as acquire, \
                mock.patch("core.music.focus_bridge.release",
                           mock.AsyncMock(return_value=(True, "resumed"))) as release:
            session = test_announce._run(scenario())

        acquire.assert_awaited_once()
        release.assert_awaited_once_with(token)
        self.assertTrue(session.focus_resumed)
        self.assertEqual("resumed", session.focus_reason)

    def test_focus_released_even_when_body_raises(self) -> None:
        self.device.on("player_get_context", test_announce._context(0))

        async def scenario():
            async with self.speaker.announce(text="你好", reason="t"):
                raise RuntimeError("body boom")

        with mock.patch("core.music.focus_bridge.enabled", return_value=True), \
                mock.patch("core.music.focus_bridge.acquire",
                           mock.AsyncMock(return_value=_token())), \
                mock.patch("core.music.focus_bridge.release",
                           mock.AsyncMock(return_value=(False, "epoch_changed"))) as release:
            with self.assertRaises(RuntimeError):
                test_announce._run(scenario())
        release.assert_awaited_once()

    def test_focus_denied_refuses_the_announcement(self) -> None:
        self.device.on("player_get_context", test_announce._context(0))

        async def scenario():
            async with self.speaker.announce(text="你好", reason="t"):
                pass

        with mock.patch("core.music.focus_bridge.enabled", return_value=True), \
                mock.patch("core.music.focus_bridge.acquire",
                           mock.AsyncMock(side_effect=FocusDenied("pause not confirmed",
                                                                  code="pause_unconfirmed"))):
            with self.assertRaises(test_announce.speaker_module.AnnounceRejected):
                test_announce._run(scenario())


class ConversationFocusTest(test_pipeline.ConversationLoopTestBase):
    async def test_reply_segments_take_and_release_focus(self) -> None:
        token = _token("f-reply")
        with mock.patch("core.music.focus_bridge.acquire",
                        mock.AsyncMock(return_value=token)) as acquire, \
                mock.patch("core.music.focus_bridge.release",
                           mock.AsyncMock(return_value=(True, "resumed"))) as release:
            played = await self.controller._speak_response_segments(
                "你好", with_send_sound=False)

        self.assertEqual(["好的"], played)
        acquire.assert_awaited_once()
        release.assert_awaited_once_with(token)


if __name__ == "__main__":
    unittest.main()
