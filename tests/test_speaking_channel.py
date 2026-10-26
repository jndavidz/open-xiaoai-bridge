"""SpeakingChannel 结构强制测试（B-29 步 2 / 评审 S4 方案 A）。

锁死的 interface 契约：
  1. 播放 API 只在 open() 产出的 session 上；重入 open() 抛 SpeakingChannelError；
  2. open() 进门即物理断录（stop_recording=False 跳过——复用已断录窗口）；
  3. play_pcm 走 sleep_playback_done 原语、空 pcm no-op、失败不抛；
  4. play_segment_tts 带令牌委托 backend、TurnCancelledError 透传、
     失败降级 speaker.play(text=...)；
  5. session 退出后可再次 open（窗口串行）。
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.services.audio.speaking_channel import (  # noqa: E402
    SpeakingChannel,
    SpeakingChannelError,
)
from core.utils.pipeline import TurnCancelledError  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.played: list[tuple[str, int | None]] = []

    async def _play_response_with_tts(self, text, tts_speaker=None, playback_token=None):
        if text == "boom":
            raise RuntimeError("tts down")
        self.played.append((text, playback_token))

    def get_tts_speaker_for_session_key(self, session_key=None):
        return None


class FakeSpeaker:
    def __init__(self):
        self.calls: list[tuple] = []

    async def play(self, text=None, url=None, buffer=None, **kwargs):
        self.calls.append(("play", text, url, buffer))


class FakeController:
    def __init__(self):
        self.backend = FakeBackend()
        self._playback_token = None
        self.LOG_MODULE = "Test Conv"
        self.stops = 0
        self.speaker = FakeSpeaker()

    async def _stop_recording(self):
        self.stops += 1


def _make():
    controller = FakeController()
    channel = SpeakingChannel(controller)
    return controller, channel


class SpeakingChannelTest(unittest.IsolatedAsyncioTestCase):
    async def test_open_stops_recording_and_playback_only_on_session(self):
        controller, channel = _make()
        with mock.patch(
            "core.services.audio.speaking_channel.get_speaker",
            return_value=controller.speaker,
        ):
            async with channel.open(reason="test") as session:
                self.assertEqual(1, controller.stops, "open 进门即断录")
                await session.play_pcm(b"\x00" * 100, reason="notify")
                self.assertEqual([("play", None, None, b"\x00" * 100)], controller.speaker.calls)
            # session 退出后可再次 open（窗口串行）
            async with channel.open(reason="test2", stop_recording=False) as session:
                self.assertEqual(1, controller.stops, "stop_recording=False 不重复断录")
                self.assertIsNotNone(session)

    async def test_reentrant_open_is_rejected(self):
        _, channel = _make()
        ctx = channel.open(reason="outer")
        async with ctx:
            with self.assertRaises(SpeakingChannelError):
                async with channel.open(reason="inner"):
                    pass

    async def test_play_pcm_empty_and_speaker_missing_are_noop(self):
        controller, channel = _make()
        async with channel.open(reason="test", stop_recording=False) as session:
            await session.play_pcm(b"", reason="empty")
            with mock.patch("core.services.audio.speaking_channel.get_speaker", return_value=None):
                await session.play_pcm(b"\x00" * 10, reason="no speaker")
        self.assertEqual([], controller.speaker.calls)

    async def test_play_pcm_failure_does_not_raise(self):
        controller, channel = _make()

        async def broken_play(**kwargs):
            raise RuntimeError("device gone")

        controller.speaker.play = broken_play
        async with channel.open(reason="test", stop_recording=False) as session:
            await session.play_pcm(b"\x00" * 10, reason="broken")
        # 不抛 = 对话不死在提示音上（与旧 _play_notify 同口径）

    async def test_play_segment_tts_delegates_with_token(self):
        controller, channel = _make()
        controller._playback_token = 42
        async with channel.open(reason="test", stop_recording=False) as session:
            await session.play_segment_tts("好的")
        self.assertEqual([("好的", 42)], controller.backend.played)

    async def test_play_segment_tts_cancelled_passes_through(self):
        controller, channel = _make()

        async def cancelled(text, tts_speaker=None, playback_token=None):
            raise TurnCancelledError(1, "stream_segment", "test")

        controller.backend._play_response_with_tts = cancelled
        async with channel.open(reason="test", stop_recording=False) as session:
            with self.assertRaises(TurnCancelledError):
                await session.play_segment_tts("段")

    async def test_play_segment_tts_failure_falls_back_to_text_play(self):
        controller, channel = _make()
        with mock.patch(
            "core.services.audio.speaking_channel.get_speaker",
            return_value=controller.speaker,
        ):
            async with channel.open(reason="test", stop_recording=False) as session:
                await session.play_segment_tts("boom")
        self.assertEqual([("play", "boom", None, None)], controller.speaker.calls)


if __name__ == "__main__":
    unittest.main()
