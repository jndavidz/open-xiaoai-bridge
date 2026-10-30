"""托管播放 EOF 判定与队列推进（B-06）契约测试。

fake clock + fake backend，纯内存/临时 SQLite：不连设备、不联网。覆盖：

  1. 单 URI 只推进一次、末条停止；不等待自然 STOPPED、不用设备 Next。
  2. 到点先 Stop 并确认；Stop 未确认 ⇒ unknown 且不推进。
  3. 暂停冻结计时；未知时长 ⇒ unknown；位置陈旧 ⇒ stalled。
  4. 回绕补偿只一次（且仅限声明支持的 backend）。
  5. 暂停/停止态 seek 明拒；外部接管 ⇒ relinquish，不再 Stop/推进。
  6. 只准备当前 entry（解析器按 entry 调用一次）。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music.model import Intent, Phase, QueueEntry, TrackRef  # noqa: E402
from core.music.player import (  # noqa: E402
    DLNA,
    RPC,
    BackendProfile,
    Decision,
    ManagedQueuePlayer,
    Unsupported,
)
from core.music.store import ManagedStore  # noqa: E402

POSITIONED = BackendProfile(name="fake-pos", position=True, seek=False, resume=True,
                            stop=True, wraparound_compensation=False)
WRAPPED = BackendProfile(name="fake-wrap", position=True, seek=False, resume=True,
                         stop=True, wraparound_compensation=True)


class FakeBackend:
    def __init__(self, profile=DLNA, *, stop_confirms: bool = True):
        self.profile = profile
        self.stop_confirms = stop_confirms
        self.played: list[str] = []
        self.stops = 0
        self.state_value = "playing"

    async def play(self, uri: str) -> bool:
        self.played.append(uri)
        self.state_value = "playing"
        return True

    async def stop(self) -> bool:
        self.stops += 1
        if self.stop_confirms:
            self.state_value = "idle"
        return True

    async def pause(self) -> bool:
        self.state_value = "paused"
        return True

    async def resume(self) -> bool:
        self.state_value = "playing"
        return True

    async def state(self) -> str:
        return self.state_value

    async def position(self) -> float | None:
        return None


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ManagedStore(Path(self._tmp.name) / "p.db")
        self.now = [0.0]
        self.session = self.store.open_session(target="lx06")
        self.sid = self.session.session_id
        self.resolved: list[str] = []

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _append(self, track_id: str, duration: float) -> QueueEntry:
        return self.store.append(self.sid, TrackRef.make("kuwo", track_id), duration_s=duration)

    async def _resolver(self, entry: QueueEntry) -> str:
        self.resolved.append(entry.entry_id)
        return f"http://cdn/{entry.ref.id}.mp3"

    def _player(self, backend: FakeBackend) -> ManagedQueuePlayer:
        return ManagedQueuePlayer(self.store, backend, self._resolver, clock=lambda: self.now[0])


class AdvanceOnceTest(_Base):
    async def test_advances_exactly_once_and_prepares_only_current(self) -> None:
        self._append("a", 10)
        self._append("b", 10)
        backend = FakeBackend()
        player = self._player(backend)

        await player.start(self.sid)
        self.assertEqual(1, len(self.resolved), "只准备当前 entry")
        self.now[0] = 9.0
        self.assertEqual(Decision.ADVANCE, await player.tick(self.sid))
        self.assertEqual(1, backend.stops, "推进前必须主动 Stop")
        self.assertEqual(1, self.store.get(self.sid).current_index)

        # 同一首不再二次推进
        self.assertEqual(Decision.WAIT, await player.tick(self.sid))

        await player.start(self.sid)
        self.assertEqual(2, len(self.resolved), "换曲后只准备新的当前 entry")
        self.assertEqual(["http://cdn/a.mp3", "http://cdn/b.mp3"], backend.played)

    async def test_stop_confirm_accepts_unknown_context(self) -> None:
        # 设备上下文丢失后 player_context 长期 unknown：stop 已下发，不能把队列卡死在 playing
        self._append("a", 5)

        class UnknownAfterStop(FakeBackend):
            async def state(self):
                return "unknown"

        backend = UnknownAfterStop()
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 4.8
        self.assertEqual(Decision.FINISH, await player.tick(self.sid))
        self.assertGreaterEqual(backend.stops, 1)

    async def test_stop_confirm_rejects_playing_state(self) -> None:
        # 设备明确仍在 playing：绝不把在播内容当已停
        self._append("a", 5)
        self._append("b", 5)

        class StillPlaying(FakeBackend):
            async def state(self):
                return "playing"

        backend = StillPlaying()
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 4.8
        self.assertEqual(Decision.UNKNOWN, await player.tick(self.sid))
        self.assertEqual(0, self.store.get(self.sid).current_index)

    async def test_concurrent_ticks_advance_once(self) -> None:
        # Device Stop 是 await 点：两个并发 tick 都看到 ADVANCE 时，只能推进一次。
        self._append("a", 10)
        self._append("b", 10)
        backend = FakeBackend()
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 9.5
        first, second = await asyncio.gather(player.tick(self.sid), player.tick(self.sid))
        self.assertIn(Decision.ADVANCE, (first, second))
        state = self.store.get(self.sid)
        self.assertEqual(1, state.current_index)
        events, _ = self.store.events_after(self.sid, 0)
        self.assertEqual(1, sum(1 for e in events if e["kind"] == "advance"))

    async def test_stop_backend_actually_stops_device(self) -> None:
        # 命令层 stop 只 bind(None) 会让设备继续出声；stop_backend 必须真下发 Stop
        # 并清掉本地准备，保证「取消后无残留」（SLO 采样暴露）。
        self._append("a", 10)
        backend = FakeBackend()
        player = self._player(backend)
        await player.start(self.sid)
        self.assertEqual("playing", backend.state_value)
        await player.stop_backend()
        self.assertGreaterEqual(backend.stops, 1)
        self.assertEqual("idle", backend.state_value)
        self.assertIsNone(player._detector)
        self.assertIsNone(player._prepared_uri)

    async def test_skip_failed_moves_on_and_marks_event(self) -> None:
        self._append("a", 10)
        self._append("b", 10)
        player = self._player(FakeBackend())
        self.assertTrue(player.skip_failed(self.sid, "kwqq resolve: http=415"))
        state = self.store.get(self.sid)
        self.assertEqual(1, state.current_index)
        self.assertEqual(Phase.PREPARING, state.phase)
        kinds = [row["kind"] for row in self.store.events_after(self.sid, 0)[0]]
        self.assertIn("advance_failed", kinds)

    async def test_skip_failed_on_last_entry_stops_visibly(self) -> None:
        self._append("only", 10)
        player = self._player(FakeBackend())
        self.assertFalse(player.skip_failed(self.sid, "kwqq resolve: http=415"))
        state = self.store.get(self.sid)
        self.assertEqual(Phase.STOPPED, state.phase)
        self.assertEqual(Intent.STOPPED, state.intent)
        self.assertEqual("advance_failed", state.stop_reason)

    async def test_skip_failed_ignored_after_user_stop(self) -> None:
        # 解析期间用户 stop：失败不能把已停止的会话重新拉回 preparing
        self._append("a", 10)
        self._append("b", 10)
        player = self._player(FakeBackend())
        self.store.transition(self.sid, lambda s: setattr(s, "intent", Intent.STOPPED))
        self.assertFalse(player.skip_failed(self.sid, "prepare failed"))
        state = self.store.get(self.sid)
        self.assertEqual(Intent.STOPPED, state.intent)
        self.assertEqual(0, state.current_index)

    async def test_resolver_supplied_duration_enables_advance(self) -> None:
        # HTTP/App 入队不带 duration：解析器（Songloft 准备链）返回验证时长后，
        # EOF 判定必须有可信时长并可推进——否则播完永远停在原曲。
        self._append("a", 0)   # 未知时长
        self._append("b", 0)
        backend = FakeBackend()

        async def resolver(entry: QueueEntry):
            self.resolved.append(entry.entry_id)
            return f"http://cdn/{entry.ref.id}.mp3", 8.0

        player = ManagedQueuePlayer(self.store, backend, resolver, clock=lambda: self.now[0])
        await player.start(self.sid)
        state = self.store.get(self.sid)
        self.assertAlmostEqual(state.entries[0].duration_s, 8.0)
        self.now[0] = 7.5
        self.assertEqual(Decision.ADVANCE, await player.tick(self.sid))
        self.assertEqual(1, self.store.get(self.sid).current_index)

    async def test_last_entry_stops_cleanly(self) -> None:
        self._append("only", 5)
        backend = FakeBackend()
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 4.5
        self.assertEqual(Decision.FINISH, await player.tick(self.sid))
        state = self.store.get(self.sid)
        self.assertEqual(Phase.STOPPED, state.phase)
        self.assertEqual(Intent.STOPPED, state.intent)
        self.assertEqual("end_of_queue", state.stop_reason)
        self.assertEqual(1, backend.stops)

    async def test_stop_unconfirmed_is_unknown_without_advance(self) -> None:
        self._append("a", 5)
        self._append("b", 5)
        backend = FakeBackend(stop_confirms=False)
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 4.5
        self.assertEqual(Decision.UNKNOWN, await player.tick(self.sid))
        self.assertEqual(0, self.store.get(self.sid).current_index, "未确认 Stop 不得推进")
        self.assertEqual(2, backend.stops, "按确认次数重试 Stop，但不重发 Play")


class TimingTest(_Base):
    async def test_pause_freezes_the_clock(self) -> None:
        self._append("a", 10)
        self._append("b", 10)
        backend = FakeBackend(RPC)     # resume 只在已声明的 backend 上放行
        player = self._player(backend)
        await player.start(self.sid)

        self.now[0] = 5.0
        await player.pause(self.sid)
        self.now[0] = 30.0
        self.assertEqual(Decision.WAIT, await player.tick(self.sid), "暂停期间不推进")

        self.now[0] = 31.0
        await player.resume(self.sid)
        self.now[0] = 36.0
        self.assertEqual(Decision.ADVANCE, await player.tick(self.sid))

    async def test_unknown_duration_is_unknown(self) -> None:
        self._append("a", 0)
        backend = FakeBackend()
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 999.0
        self.assertEqual(Decision.UNKNOWN, await player.tick(self.sid))
        self.assertEqual(0, backend.stops)

    async def test_stale_position_stalls(self) -> None:
        self._append("a", 100)
        backend = FakeBackend(POSITIONED)
        player = self._player(backend)
        await player.start(self.sid)
        player.update_position(5.0)          # t=0 的位置
        self.now[0] = 20.0                    # 位置已陈旧 > stall(10s)，而总时长未到
        self.assertEqual(Decision.STALLED, await player.tick(self.sid))

    async def test_wraparound_compensates_once(self) -> None:
        self._append("a", 100)
        self._append("b", 100)
        backend = FakeBackend(WRAPPED)
        player = self._player(backend)
        await player.start(self.sid)
        player.update_position(80.0)
        self.now[0] = 1.0
        player.update_position(5.0)           # 回绕
        self.assertEqual(Decision.ADVANCE, await player.tick(self.sid))
        self.assertEqual(1, self.store.get(self.sid).current_index)


class RefusalTest(_Base):
    async def test_seek_is_unsupported_when_backend_lacks_it(self) -> None:
        self._append("a", 10)
        backend = FakeBackend(DLNA)
        player = self._player(backend)
        await player.start(self.sid)
        with self.assertRaises(Unsupported):
            await player.seek(self.sid, 5.0)

    async def test_seek_after_stop_does_not_play(self) -> None:
        self._append("a", 5)
        backend = FakeBackend(POSITIONED)
        player = self._player(backend)
        await player.start(self.sid)
        self.now[0] = 4.5
        await player.tick(self.sid)           # 末条：已停止
        played_before = list(backend.played)
        with self.assertRaises(Unsupported):
            await player.seek(self.sid, 1.0)
        self.assertEqual(played_before, backend.played, "迟到 seek 不得偷偷 Play")

    async def test_external_takeover_relinquishes(self) -> None:
        self._append("a", 5)
        self._append("b", 5)
        backend = FakeBackend()
        player = self._player(backend)
        await player.start(self.sid)
        player.on_external_takeover()
        self.now[0] = 4.5
        self.assertEqual(Decision.RELINQUISH, await player.tick(self.sid))
        self.assertEqual(0, backend.stops, "外部接管后不抢回、不 Stop")
        self.assertEqual(0, self.store.get(self.sid).current_index)

    async def test_rpc_profile_claims_resume_but_not_seek(self) -> None:
        self.assertTrue(RPC.resume)
        self.assertFalse(RPC.seek)
        self.assertFalse(RPC.position)
        self.assertTrue(DLNA.stop)
        self.assertFalse(DLNA.resume)


class _Result:
    def __init__(self, ok=True, readback=None):
        self.ok = ok
        self.readback = readback or {}


class _FakeDevice:
    def __init__(self, status=1):
        self.status = status
        self.calls: list[tuple] = []

    async def play_url(self, uri):
        self.calls.append(("play_url", uri))
        return _Result(True)

    async def stop_audio(self):
        self.calls.append(("stop_audio",))
        return _Result(True)

    async def pause(self):
        self.calls.append(("pause",))
        return _Result(True)

    async def playback_resume(self):
        self.calls.append(("playback_resume",))
        return _Result(True)

    async def player_context(self):
        self.calls.append(("player_context",))
        return _Result(True, {"status": self.status})


class DeviceBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_dlna_backend_uses_play_url_and_stop_audio(self) -> None:
        from core.music.backends import DlnaBackend

        device = _FakeDevice(status=1)
        backend = DlnaBackend(device=device)
        self.assertTrue(await backend.play("http://cdn/a.mp3"))
        self.assertEqual("playing", await backend.state())
        self.assertTrue(await backend.stop())
        self.assertEqual([("play_url", "http://cdn/a.mp3"), ("player_context",),
                          ("stop_audio",)], device.calls)
        device.status = 0
        self.assertEqual("idle", await backend.state())
        device.status = 99
        self.assertEqual("unknown", await backend.state())

    async def test_rpc_backend_declares_resume(self) -> None:
        from core.music.backends import RpcBackend

        device = _FakeDevice()
        backend = RpcBackend(device=device)
        self.assertTrue(await backend.resume())
        self.assertEqual([("playback_resume",)], device.calls)
        self.assertTrue(backend.profile.resume)
        self.assertFalse(backend.profile.seek)

    async def test_backends_do_not_claim_verified_position(self) -> None:
        from core.music.backends import DlnaBackend, RpcBackend

        self.assertIsNone(await DlnaBackend(device=_FakeDevice()).position())
        self.assertIsNone(await RpcBackend(device=_FakeDevice()).position())


if __name__ == "__main__":
    unittest.main()
