"""TTS focus 与恢复资格（B-07）契约测试。

纯内存/临时 SQLite + fake provider：不连设备、不联网。覆盖：

  1. 先暂停并确认才发令牌；暂停未确认/会话已停止 ⇒ 拒绝。
  2. 恢复资格绑定 session/epoch/entry/focus token：stop、外部接管、二次播报换令牌
     都会让旧令牌失效，且绝不偷偷 Play。
  3. 每个 provider 独立结果（ok/failed/cancelled/unknown 不合并）；弱完成先安全停止。
  4. backend 未声明 resume 时如实返回未恢复。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music.model import Intent, Phase, TrackRef  # noqa: E402
from core.music.service import ManagedService  # noqa: E402
from core.music.store import ManagedStore  # noqa: E402
from core.music.tts_focus import FocusDenied, SpeakResult, TtsFocus  # noqa: E402


class FakeBackend:
    def __init__(self, state: str = "playing") -> None:
        self.state_value = state

    async def state(self) -> str:
        return self.state_value


class FakePlayer:
    """最小 player：pause/resume 改 store（含 epoch bump），resume 可声明不支持。"""

    def __init__(self, store, backend, *, resume_supported: bool = True,
                 confirm_pause: bool = True) -> None:
        self.store = store
        self.backend = backend
        self.resume_supported = resume_supported
        self.confirm_pause = confirm_pause
        self.paused = 0
        self.resumed = 0

    async def pause(self, session_id: str) -> None:
        self.paused += 1
        if self.confirm_pause:
            self.backend.state_value = "paused"

        def mutate(state):
            state.epoch += 1
            state.phase = Phase.PAUSED
            state.intent = Intent.PAUSED
            state.auto_advance = False

        self.store.transition(session_id, mutate)

    async def ensure_resumed(self, session_id: str, *, verify_timeout_s: float = 0.8) -> str:
        if not self.resume_supported:
            from core.music.player import Unsupported

            raise Unsupported("no verified resume")

        def mutate(state):
            state.epoch += 1
            state.phase = Phase.PLAYING
            state.intent = Intent.PLAYING

        self.store.transition(session_id, mutate)
        self.resumed += 1
        self.backend.state_value = "playing"
        return "resumed"

    async def resume(self, session_id: str) -> None:
        if not self.resume_supported:
            from core.music.player import Unsupported

            raise Unsupported("no verified resume")
        self.resumed += 1
        self.backend.state_value = "playing"

        def mutate(state):
            state.epoch += 1
            state.phase = Phase.PLAYING
            state.intent = Intent.PLAYING

        self.store.transition(session_id, mutate)


class FakeSpeaker:
    def __init__(self, name: str, *, status: str = "ok", quality: str | None = "strong",
                 starts: bool = True) -> None:
        self.name = name
        self.status = status
        self.quality = quality
        self.starts = starts
        self.spoken: list[str] = []
        self.cancels = 0

    async def speak(self, text: str, *, token) -> SpeakResult:
        self.spoken.append(text)
        return SpeakResult(self.name, self.status, self.starts, self.quality)

    async def cancel(self, token) -> bool:
        self.cancels += 1
        return True


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ManagedStore(Path(self._tmp.name) / "f.db")
        self.service = ManagedService(store=self.store)
        snapshot = self.service.open_session(target="lx06")
        self.sid = snapshot["session_id"]
        self.store.append(self.sid, TrackRef.make("kuwo", "1"), duration_s=200)
        self.store.mark_playing(self.sid)
        self.backend = FakeBackend("playing")
        self.player = FakePlayer(self.store, self.backend)
        self.focus = TtsFocus(self.service, self.player,
                              confirm_timeout_s=0.05, poll_s=0.01)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _acquire(self, provider="xiaoai"):
        import asyncio

        return asyncio.run(self.focus.acquire(self.sid, provider=provider))

    def _release(self, token):
        import asyncio

        return asyncio.run(self.focus.release(token))


class AcquireTest(_Base):
    def test_acquire_pauses_and_confirms_before_issuing(self) -> None:
        token = self._acquire()
        self.assertEqual(1, self.player.paused)
        state = self.store.get(self.sid)
        self.assertEqual(token.token, state.focus_token)
        self.assertEqual(state.epoch, token.epoch)
        self.assertEqual(Phase.PAUSED, state.phase)

    def test_unconfirmed_pause_is_denied(self) -> None:
        self.player.confirm_pause = False
        with self.assertRaises(FocusDenied):
            self._acquire()
        self.assertIsNone(self.store.get(self.sid).focus_token)

    def test_stopped_session_is_denied(self) -> None:
        self.service.command(self.sid, kind="stop", principal="p", idem_key="s")
        with self.assertRaises(FocusDenied):
            self._acquire()

    def test_pause_confirm_has_a_settle_window(self) -> None:
        """设备暂停不是瞬时回读：允许在结算窗口内看到 paused。"""

        class SlowPausePlayer(FakePlayer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.polls = 0

            async def pause(self, session_id):
                await super().pause(session_id)
                self.backend.state_value = "playing"   # 刚下发时还没生效

        slow = SlowPausePlayer(self.store, self.backend)
        focus = TtsFocus(self.service, slow, confirm_timeout_s=0.5, poll_s=0.01)
        original_state = slow.backend.state

        async def state():
            slow.polls += 1
            if slow.polls >= 3:
                return "paused"
            return await original_state()

        slow.backend.state = state

        import asyncio

        token = asyncio.run(focus.acquire(self.sid, provider="xiaoai"))
        self.assertIsNotNone(token)
        self.assertGreaterEqual(slow.polls, 3)

    def test_idle_after_pause_is_treated_as_paused(self) -> None:
        # 起播后立刻播报：设备暂停后可能先回 idle。若按「音乐已结束」放弃焦点，
        # phase 会永久停在 paused；这里必须继续取得焦点，释放时由 ensure_resumed
        # 回读并 restart 当前 entry。
        class EndingPlayer(FakePlayer):
            async def pause(self, session_id):
                await super().pause(session_id)
                self.backend.state_value = "idle"

        focus = TtsFocus(self.service, EndingPlayer(self.store, self.backend),
                         confirm_timeout_s=0.1, poll_s=0.01)
        import asyncio

        token = asyncio.run(focus.acquire(self.sid, provider="xiaoai"))
        self.assertIsNotNone(token)
        self.assertEqual(token.token, self.store.get(self.sid).focus_token)

    def test_second_announcement_reissues_the_token(self) -> None:
        first = self._acquire()
        second = self._acquire()
        self.assertNotEqual(first.token, second.token)
        self.assertEqual(1, self.player.paused, "连续播报复用同一次暂停")
        self.assertEqual(second.token, self.store.get(self.sid).focus_token)


class ReleaseTest(_Base):
    def test_valid_focus_resumes(self) -> None:
        token = self._acquire()
        resumed, reason = self._release(token)
        self.assertTrue(resumed)
        self.assertEqual("resumed", reason)
        self.assertEqual(1, self.player.resumed)
        self.assertEqual(Phase.PLAYING, self.store.get(self.sid).phase)

    def test_superseded_token_never_resumes(self) -> None:
        first = self._acquire()
        self._acquire()
        resumed, reason = self._release(first)
        self.assertFalse(resumed)
        self.assertEqual("focus_superseded", reason)
        self.assertEqual(0, self.player.resumed)

    def test_user_stop_invalidates_resume(self) -> None:
        token = self._acquire()
        self.service.command(self.sid, kind="stop", principal="p", idem_key="stop-focus")
        resumed, reason = self._release(token)
        self.assertFalse(resumed)
        self.assertIn(reason, ("epoch_changed", "stopped"))
        self.assertEqual(0, self.player.resumed)

    def test_external_takeover_invalidates_resume(self) -> None:
        token = self._acquire()
        self.backend.state_value = "playing"      # 外部接管：音乐不再处于暂停
        resumed, reason = self._release(token)
        self.assertFalse(resumed)
        self.assertEqual("external_takeover", reason)
        self.assertEqual(0, self.player.resumed)

    def test_unknown_state_still_attempts_resume(self) -> None:
        # TTS 刚结束时 player_context 可能读不到（unknown），不能当外部接管
        token = self._acquire()
        self.backend.state_value = "unknown"
        resumed, reason = self._release(token)
        self.assertTrue(resumed)
        self.assertEqual("resumed", reason)
        self.assertEqual(1, self.player.resumed)

    def test_entry_change_invalidates_resume(self) -> None:
        token = self._acquire()
        # 新曲起播（等价于用户点了别的歌）：代次与当前项都变
        self.store.append(self.sid, TrackRef.make("kuwo", "2"), duration_s=100)
        self.store.set_current(self.sid, self.store.get(self.sid).entries[1].entry_id)
        self.store.bump_epoch(self.sid)
        resumed, reason = self._release(token)
        self.assertFalse(resumed)
        self.assertIn(reason, ("epoch_changed", "entry_changed"))

    def test_unsupported_resume_is_reported_honestly(self) -> None:
        self.player.resume_supported = False
        token = self._acquire()
        resumed, reason = self._release(token)
        self.assertFalse(resumed)
        self.assertEqual("resume_unsupported", reason)


class ProviderResultTest(_Base):
    def test_each_provider_reports_independently(self) -> None:
        import asyncio

        token = self._acquire()
        speakers = [
            FakeSpeaker("doubao"),
            FakeSpeaker("openai", status="failed"),
            FakeSpeaker("mlx_audio", status="cancelled"),
            FakeSpeaker("xiaoai", status="unknown"),
        ]
        results = asyncio.run(self.focus.speak(token, "你好", speakers))
        self.assertEqual(["doubao", "openai", "mlx_audio", "xiaoai"],
                         [r.provider for r in results])
        self.assertEqual(["ok", "failed", "cancelled", "unknown"],
                         [r.status for r in results])
        self.assertFalse(any(r.safe_stopped for r in results[:3]))
        self.assertTrue(results[3].safe_stopped, "unknown 必须先安全停止")
        self.assertEqual(1, speakers[3].cancels)

    def test_weak_completion_triggers_safe_stop(self) -> None:
        import asyncio

        token = self._acquire()
        weak = FakeSpeaker("doubao", status="ok", quality="weak")
        failed = FakeSpeaker("mlx_audio", status="failed")
        results = asyncio.run(self.focus.speak(token, "长文本", [weak, failed]))
        self.assertTrue(results[0].safe_stopped)
        self.assertEqual(1, weak.cancels)
        self.assertEqual(0, failed.cancels, "失败本身不需要再取消")

    def test_repeated_announcements_keep_independent_results(self) -> None:
        import asyncio

        token = self._acquire()
        speakers = [FakeSpeaker("doubao"), FakeSpeaker("xiaoai")]
        first = asyncio.run(self.focus.speak(token, "第一段", speakers))
        second = asyncio.run(self.focus.speak(token, "第二段", speakers))
        self.assertEqual(2, len(first))
        self.assertEqual(2, len(second))
        self.assertEqual(["第一段", "第二段"], speakers[0].spoken)
        self.assertTrue(all(r.status == "ok" for r in first + second))

    def test_cancel_calls_every_provider(self) -> None:
        import asyncio

        token = self._acquire()
        speakers = [FakeSpeaker("doubao"), FakeSpeaker("openai")]
        self.assertEqual(2, asyncio.run(self.focus.cancel(token, speakers)))
        self.assertEqual([1, 1], [s.cancels for s in speakers])


class ProviderAdapterTest(unittest.IsolatedAsyncioTestCase):
    """四个 provider 各自的令牌传递与取消语义（不依赖真实 router/Rust）。"""

    def _token(self):
        from core.music.tts_focus import FocusToken

        return FocusToken("f-1", "ms-1", 1, "e-1", 10.0, "doubao", 0.0)

    async def test_router_providers_pass_their_own_playback_token(self) -> None:
        from core.music.tts_providers import RouterTtsSpeaker

        calls: list[tuple] = []
        stops: list[int] = []

        async def play(text, provider, token):
            calls.append((provider, token))

        for name in ("doubao", "openai", "mlx_audio"):
            speaker = RouterTtsSpeaker(name, play=play, stop=lambda t: stops.append(t) or True)
            result = await speaker.speak("你好", token=self._token())
            self.assertEqual(name, result.provider)
            self.assertEqual("ok", result.status)
            self.assertTrue(await speaker.cancel(self._token()) or True)

        providers = [c[0] for c in calls]
        self.assertEqual(["doubao", "openai", "mlx_audio"], providers)
        self.assertEqual(3, len({c[1] for c in calls}), "每个 provider 独立令牌")
        self.assertEqual(3, len(stops), "取消按各自令牌停止")

    async def test_router_failure_is_reported_per_provider(self) -> None:
        from core.music.tts_providers import RouterTtsSpeaker

        async def boom(text, provider, token):
            raise RuntimeError("upstream 500")

        speaker = RouterTtsSpeaker("openai", play=boom, stop=lambda t: True)
        result = await speaker.speak("你好", token=self._token())
        self.assertEqual("failed", result.status)
        self.assertIn("upstream 500", result.reason)
        self.assertFalse(await speaker.cancel(self._token()), "失败后没有可取消的令牌")

    async def test_xiaoai_adapter_uses_announce_and_device_stop(self) -> None:
        from core.music.tts_providers import XiaoaiScriptSpeaker

        spoken: list[str] = []
        stopped: list[bool] = []

        async def announce(text):
            spoken.append(text)
            return True

        async def stop():
            stopped.append(True)
            return True

        speaker = XiaoaiScriptSpeaker(announce=announce, stop=stop)
        result = await speaker.speak("客厅音响已就绪", token=self._token())
        self.assertEqual("xiaoai", result.provider)
        self.assertEqual("ok", result.status)
        self.assertTrue(await speaker.cancel(self._token()))
        self.assertEqual(["客厅音响已就绪"], spoken)
        self.assertEqual([True], stopped)

    async def test_xiaoai_unavailable_is_failed_not_ok(self) -> None:
        from core.music.tts_providers import XiaoaiScriptSpeaker

        async def announce(_text):
            return False

        speaker = XiaoaiScriptSpeaker(announce=announce)
        result = await speaker.speak("你好", token=self._token())
        self.assertEqual("failed", result.status)
        self.assertEqual("announce_unavailable", result.reason)


if __name__ == "__main__":
    unittest.main()
