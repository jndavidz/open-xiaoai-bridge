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

# 步 3 门控测试会拉真 core.services.speaker（AnnounceRejected）→ 其顶层 import
# open_xiaoai_server；单文件运行时无前置测试替它注入 native 桩。
sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

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
        self.bypass_plays: list[tuple[str, str]] = []

    async def _stop_recording(self):
        self.stops += 1

    async def _play_bypass_text(self, text, *, reason):
        self.bypass_plays.append((text, reason))


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

    async def test_play_segment_tts_failure_falls_back_via_bypass_gate(self):
        controller, channel = _make()
        async with channel.open(reason="test", stop_recording=False) as session:
            await session.play_segment_tts("boom")
        self.assertEqual([("boom", "tts fallback")], controller.bypass_plays)


class BypassTextGateTest(unittest.IsolatedAsyncioTestCase):
    """_play_bypass_text 门控语义（B-29 步 3）：默认裸 play，开 = announce 包裹。

    直接以非绑定方式调真方法（FakeController 的同名桩只供句段 fallback 用）。
    """

    def _controller(self, enabled: bool):
        from core.services.speaker import AnnounceRejected

        controller = types.SimpleNamespace(
            LOG_MODULE="Test Conv",
            speaker=FakeSpeaker(),
            config=types.SimpleNamespace(
                get_app_config=lambda path, default=None: (
                    enabled if path == "conversation.bypass_play_via_announce" else default
                )
            ),
        )
        return controller, AnnounceRejected

    async def test_default_off_plays_directly(self):
        from core.external_conversation import ExternalConversationController

        controller, _ = self._controller(enabled=False)
        ann_calls: list = []

        class Announcer:
            def announce(self, *, text, reason, wait, gate_seconds):
                ann_calls.append(text)
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        controller.speaker.announce = Announcer().announce
        with mock.patch(
            "core.external_conversation.get_speaker", return_value=controller.speaker
        ):
            await ExternalConversationController._play_bypass_text(
                controller, "抱歉，我没有收到回复", reason="r1"
            )

        self.assertEqual([("play", "抱歉，我没有收到回复", None, None)], controller.speaker.calls)
        self.assertEqual([], ann_calls, "默认关：不经 announce")

    async def test_enabled_wraps_in_announce_and_rejects_visibly(self):
        from core.external_conversation import ExternalConversationController

        controller, AnnounceRejected = self._controller(enabled=True)
        opened: list[str] = []

        class Announcer:
            def announce(self, *, text, reason, wait, gate_seconds):
                opened.append(text)
                if text == "高保真模式已开启":
                    raise AnnounceRejected("高保真模式")
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        controller.speaker.announce = Announcer().announce
        with mock.patch(
            "core.external_conversation.get_speaker", return_value=controller.speaker
        ):
            play = ExternalConversationController._play_bypass_text
            await play(controller, "好的", reason="r1")
            await play(controller, "高保真模式已开启", reason="r2")

        self.assertEqual(["好的", "高保真模式已开启"], opened, "两条都尝试 announce")
        self.assertEqual([("play", "好的", None, None)], controller.speaker.calls,
                         "拒绝的那条不播（词表检查默认拒绝，留 WARNING）")


if __name__ == "__main__":
    unittest.main()
