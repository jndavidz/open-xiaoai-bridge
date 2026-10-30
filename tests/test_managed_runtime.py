"""托管运行期 tick 与 song_request 迁移桥（B-06 接线）测试。

fake player/service：不连设备、不联网。覆盖：

  1. tick 到点推进后自动起播下一条；末条不再起播。
  2. 循环 start/stop 幂等，异常不杀循环。
  3. song_request flag：关 = 走旧路径（不建托管会话）；开 = 建会话、入队、起播当前项。
  4. 托管命令 next/stop 落到同一会话；stop 同时停循环并解绑。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music import song_bridge  # noqa: E402
from core.music.model import Intent, Phase, TrackRef  # noqa: E402
from core.music.player import RPC, Decision  # noqa: E402
from core.music.runtime import ManagedRuntime, get_runtime, set_runtime  # noqa: E402
from core.music.service import ManagedService  # noqa: E402
from core.music.store import ManagedStore  # noqa: E402


class FakeBackendState:
    def __init__(self, value: str = "paused"):
        self.value = value

    async def state(self) -> str:
        return self.value


class FakePlayer:
    profile = RPC

    def __init__(self, decisions: list[Decision]):
        self.decisions = decisions
        self.started: list[str] = []
        self.restarted = 0
        self.paused = 0
        self.resumed = 0
        self.pause_error: Exception | None = None
        self.backend = FakeBackendState()
        self.fake_no_audio = False
        self.resume_noop = False
        self.ticks = 0

    async def play(self, uri: str) -> bool:
        self.started.append(uri)
        return True

    async def start(self, session_id: str) -> bool:
        self.started.append(session_id)
        return True

    async def restart(self, session_id: str) -> bool:
        self.restarted += 1
        self.backend.value = "playing"
        return await self.start(session_id)

    async def ensure_resumed(self, session_id: str, *, verify_timeout_s: float = 0.8) -> str:
        if self.backend.value == "paused" and not self.fake_no_audio:
            self.resumed += 1
            self.backend.value = "playing"
            return "resumed"
        await self.restart(session_id)
        return "restarted"

    async def state(self) -> str:
        return self.backend.value

    async def resume(self) -> bool:
        if not self.resume_noop:
            self.backend.value = "playing"
        return True

    async def pause(self) -> bool:
        self.backend.value = "paused"
        return True

    async def stop(self) -> bool:
        self.backend.value = "idle"
        return True

    async def pause_backend(self) -> None:
        if self.pause_error:
            raise self.pause_error
        self.paused += 1

    async def resume_backend(self) -> None:
        self.resumed += 1
        if not self.resume_noop:
            self.backend.value = "playing"

    async def tick(self, session_id: str) -> Decision:
        self.ticks += 1
        return self.decisions.pop(0) if self.decisions else Decision.WAIT


class RuntimeTickTest(unittest.IsolatedAsyncioTestCase):
    async def test_tick_advances_and_starts_next_entry(self) -> None:
        player = FakePlayer([Decision.ADVANCE])
        runtime = ManagedRuntime(player)
        runtime.bind("ms-1")
        self.assertEqual(Decision.ADVANCE, await runtime.tick_once())
        self.assertEqual(["ms-1"], player.started, "推进后必须起播下一条")

    async def test_wait_does_not_start(self) -> None:
        player = FakePlayer([Decision.WAIT])
        runtime = ManagedRuntime(player)
        runtime.bind("ms-1")
        self.assertEqual(Decision.WAIT, await runtime.tick_once())
        self.assertEqual([], player.started)

    async def test_unbound_runtime_is_a_noop(self) -> None:
        player = FakePlayer([Decision.ADVANCE])
        runtime = ManagedRuntime(player)
        self.assertIsNone(await runtime.tick_once())
        self.assertEqual(0, player.ticks)

    async def test_loop_starts_and_stops_idempotently(self) -> None:
        player = FakePlayer([])
        runtime = ManagedRuntime(player, interval_s=0.01)
        runtime.bind("ms-1")
        await runtime.start()
        await runtime.start()          # 幂等
        await asyncio.sleep(0.05)
        self.assertTrue(runtime.ticks >= 1)
        await runtime.stop()
        await runtime.stop()
        self.assertFalse(runtime.running)

    async def test_tick_errors_do_not_kill_the_loop(self) -> None:
        class Boom(FakePlayer):
            async def tick(self, session_id):
                self.ticks += 1
                if self.ticks == 1:
                    raise RuntimeError("boom")
                return Decision.WAIT

        player = Boom([])
        runtime = ManagedRuntime(player, interval_s=0.01)
        runtime.bind("ms-1")
        await runtime.start()
        await asyncio.sleep(0.06)
        await runtime.stop()
        self.assertGreaterEqual(player.ticks, 2)


class SongBridgeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ManagedStore(Path(self._tmp.name) / "b.db")
        self.service = ManagedService(store=self.store)
        self.player = FakePlayer([])
        self.runtime = ManagedRuntime(self.player, service=self.service, interval_s=10.0)
        set_runtime(self.runtime)

    def tearDown(self) -> None:
        set_runtime(None)
        self.store.close()
        self._tmp.cleanup()

    def _items(self):
        return [
            {"id": "a", "source": "kuwo", "name": "A", "duration_s": 200},
            {"id": "b", "source": "kuwo", "name": "B", "duration_s": 180},
        ]

    async def test_flag_off_keeps_legacy_path(self) -> None:
        with mock.patch.object(song_bridge._sr, "_cfg", lambda key, default=None: False):
            self.assertFalse(song_bridge.managed_enabled())

    async def test_play_items_creates_session_queue_and_starts(self) -> None:
        result = await song_bridge.play_items(self._items())
        self.assertIn("托管队列", result)
        sid = self.runtime.session_id
        self.assertIsNotNone(sid)
        state = self.store.get(sid)
        self.assertEqual(2, len(state.entries))
        self.assertEqual(200.0, state.entries[0].duration_s)
        self.assertEqual(0, state.current_index)
        self.assertEqual([sid], self.player.started)
        self.assertTrue(self.runtime.running)

    async def test_next_command_moves_and_starts_next(self) -> None:
        await song_bridge.play_items(self._items())
        sid = self.runtime.session_id
        await song_bridge.command("next")
        self.assertEqual(1, self.store.get(sid).current_index)
        self.assertEqual([sid, sid], self.player.started)

    async def test_stop_command_clears_intent_and_unbinds(self) -> None:
        await song_bridge.play_items(self._items())
        sid = self.runtime.session_id
        await song_bridge.command("stop")
        self.assertEqual(Intent.STOPPED, self.store.get(sid).intent)
        self.assertIsNone(self.runtime.session_id)
        self.assertFalse(self.runtime.running)

    async def test_pause_and_resume_drive_the_device_and_mark_observed(self) -> None:
        await song_bridge.play_items(self._items())
        sid = self.runtime.session_id
        pause = await song_bridge.command("pause")
        self.assertIn("pause", pause)
        self.assertEqual(1, self.player.paused, "pause 必须真的下发到设备")
        self.assertEqual("observed", self._receipt_state("pause"))

        resume = await song_bridge.command("resume")
        self.assertIn("resume", resume)
        self.assertEqual(1, self.player.resumed, "暂停态 resume 必须真的恢复播放")

    async def test_resume_verifies_and_restarts_on_fake_success(self) -> None:
        await song_bridge.play_items(self._items())
        self.player.fake_no_audio = True       # play 返回成功但没有音频
        result = await song_bridge.command("resume")
        self.assertIn("resume", result)
        self.assertEqual(1, self.player.restarted)

    async def test_player_ensure_resumed_restarts_when_play_is_a_noop(self) -> None:
        from core.music.player import ManagedQueuePlayer

        async def resolver(entry):
            return "http://cdn/x.mp3"

        player = ManagedQueuePlayer(self.store, self.player, resolver, clock=lambda: 0.0)
        sid = self.service.open_session(target="lx06")["session_id"]
        self.store.append(sid, TrackRef.make("kuwo", "7"), duration_s=10)
        await player.start(sid)
        self.player.backend.value = "paused"
        self.player.resume_noop = True
        self.assertEqual("restarted", await player.ensure_resumed(sid, verify_timeout_s=0.0))
        self.assertEqual(2, len(self.player.started), "起播一次 + 重起一次")

    async def test_resume_restarts_when_device_context_was_lost(self) -> None:
        await song_bridge.play_items(self._items())
        self.player.backend.value = "idle"      # 唤醒/TTS 已把媒体上下文顶掉
        result = await song_bridge.command("resume")
        self.assertIn("resume", result)
        self.assertEqual(1, self.player.restarted, "上下文丢失时必须显式重新起播")
        self.assertEqual(0, self.player.resumed, "mphelper play 对已丢失上下文是无操作")
        self.assertEqual("observed", self._receipt_state("resume"))

    def _receipt_state(self, kind: str) -> str:
        # 从事件流里找到最近一次该命令的 receipt，再查它的状态
        sid = self.runtime.session_id
        events, _ = self.store.events_after(sid, 0)
        receipt_id = [e for e in events if e.get("kind") == "command"
                      and e.get("command") == kind][-1]["receipt_id"]
        return self.store.receipt_by_id(receipt_id)["state"]

    async def test_device_rejection_is_reported_not_faked(self) -> None:
        await song_bridge.play_items(self._items())
        self.player.pause_error = RuntimeError("device busy")
        result = await song_bridge.command("pause")
        self.assertIn("未能执行", result)
        self.assertEqual("failed", self._receipt_state("pause"))

    async def test_restart_re_resolves_the_uri(self) -> None:
        from core.music.player import ManagedQueuePlayer

        store = self.store
        calls: list[str] = []

        async def resolver(entry):
            calls.append(entry.entry_id)
            return "http://cdn/x.mp3"

        player = ManagedQueuePlayer(store, self.player, resolver, clock=lambda: 0.0)
        sid = self.service.open_session(target="lx06")["session_id"]
        store.append(sid, TrackRef.make("kuwo", "9"), duration_s=10)
        await player.start(sid)
        await player.restart(sid)
        self.assertEqual(2, len(calls), "重启必须重新解析直链，不能复用可能过期的 URI")

    async def test_commands_without_a_session_are_explicit(self) -> None:
        self.assertEqual("托管队列还没有内容", await song_bridge.command("pause"))

    async def test_play_items_without_candidates_is_rejected(self) -> None:
        self.assertEqual("没有可播放的曲目", await song_bridge.play_items([]))

    async def test_build_runtime_restores_old_sessions_as_recovery_pending(self) -> None:
        path = Path(self._tmp.name) / "restore.db"

        def factory():
            return ManagedStore(path)

        song_bridge._reset_for_tests()   # 让两次构建都走 factory（而不是 setUp 的运行时）
        with mock.patch.object(song_bridge, "ManagedStore", factory):
            first = song_bridge.build_runtime()
            sid = first.service.open_session(target="lx06")["session_id"]
            song_bridge._reset_for_tests()
            second = song_bridge.build_runtime()
            restored = second.service.store.get(sid)
        self.assertEqual(Phase.RECOVERY_PENDING, restored.phase)
        self.assertIsNone(restored.recovery_token)


class SongRequestDispatchTest(unittest.IsolatedAsyncioTestCase):
    """flag 决定 run() 走托管还是旧队列（回滚模式）。"""

    async def test_managed_flag_routes_run_to_the_bridge(self) -> None:
        from core.tools import song_request as sr

        collect = mock.AsyncMock(return_value=[{"id": "a", "source": "kuwo"}])
        play = mock.AsyncMock(return_value="托管队列已接管（1 首）")
        with mock.patch.object(sr, "_cfg", lambda key, default=None: True), \
                mock.patch.object(sr, "_managed_collect", collect), \
                mock.patch("core.music.song_bridge.managed_enabled", return_value=True), \
                mock.patch("core.music.song_bridge.play_items", play):
            result = await sr.run({"action": "play", "query": "晴天"})
        self.assertIn("托管队列", result)
        collect.assert_awaited_once()
        play.assert_awaited_once()

    async def test_managed_stop_ends_the_conversation_turn(self) -> None:
        from core.tools import song_request as sr

        with mock.patch.object(sr, "_cfg", lambda key, default=None: True), \
                mock.patch("core.music.song_bridge.managed_enabled", return_value=True), \
                mock.patch("core.music.song_bridge.command",
                           mock.AsyncMock(return_value="托管命令已受理（stop, applied）")), \
                mock.patch("core.tools.registry.suppress_reply") as suppress:
            result = await sr.run({"action": "stop"})
        self.assertIn("stop", result)
        suppress.assert_called_once()

    async def test_managed_resume_ends_the_conversation_turn(self) -> None:
        from core.tools import song_request as sr

        with mock.patch.object(sr, "_cfg", lambda key, default=None: True), \
                mock.patch("core.music.song_bridge.managed_enabled", return_value=True), \
                mock.patch("core.music.song_bridge.command",
                           mock.AsyncMock(return_value="托管命令已受理（resume, accepted）")), \
                mock.patch("core.tools.registry.suppress_reply") as suppress:
            result = await sr.run({"action": "resume"})
        self.assertIn("resume", result)
        suppress.assert_called_once()

    async def test_flag_off_uses_the_legacy_queue(self) -> None:
        from core.tools import song_request as sr

        legacy = mock.AsyncMock(return_value="legacy-ok")
        with mock.patch.object(sr, "_cfg", lambda key, default=None: True), \
                mock.patch.object(sr, "_do_play", legacy), \
                mock.patch("core.music.song_bridge.managed_enabled", return_value=False):
            result = await sr.run({"action": "play", "query": "晴天"})
        self.assertEqual("legacy-ok", result)
        legacy.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
