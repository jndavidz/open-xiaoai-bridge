"""HTTP/App 命令的设备执行侧（B-08）。

`ManagedAPI.post_command` 只落回执，设备动作交给 `song_bridge.apply_command`
异步执行并回收 observed/failed。这里用假 runtime 验证：

  1. play 绑定会话、起播并推进回执；
  2. 空队列 / 未绑定会话 / 设备拒绝都如实记 failed，不假装成功；
  3. next/prev 重起当前项，stop 停 tick 并解绑；
  4. ManagedAPI 只对非重放命令调度设备执行，且异常不会冒到 HTTP 层。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.music import song_bridge  # noqa: E402
from core.music.model import ManagedError, Phase, TrackRef  # noqa: E402
from core.music.player import RPC, ManagedQueuePlayer  # noqa: E402
from core.music.runtime import ManagedRuntime  # noqa: E402
from core.music.service import ManagedService  # noqa: E402
from core.music.store import ManagedStore  # noqa: E402


class FakePlayer:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started: list[str] = []
        self.paused = 0
        self.resumed = 0
        self.skip_calls: list[str] = []
        self.has_next_after_skip = False

    async def start(self, session_id: str, **_: object) -> bool:
        if self.fail_start:
            raise ManagedError("backend rejected playback")
        self.started.append(session_id)
        return True

    def skip_failed(self, session_id: str, reason: str) -> bool:
        """与 ManagedQueuePlayer.skip_failed 同签名：返回是否还有下一条。"""

        self.skip_calls.append(reason)
        return self.has_next_after_skip

    async def pause_backend(self) -> None:
        self.paused += 1

    async def stop_backend(self) -> None:
        self.stopped = getattr(self, "stopped", 0) + 1

    async def ensure_resumed(self, session_id: str) -> str:
        self.resumed += 1
        return "resumed"


class FakeRuntime:
    def __init__(self, service: ManagedService, player: FakePlayer) -> None:
        self.service = service
        self.player = player
        self.session_id: str | None = None
        self.ticking = 0
        self.stopped = 0

    def bind(self, session_id: str | None) -> None:
        self.session_id = session_id

    async def start_current(self, session_id: str) -> bool:
        """真实 ManagedRuntime.start_current 的替身：失败即走 skip 语义。"""

        try:
            await self.player.start(session_id)
            return True
        except ManagedError as exc:
            return self.player.skip_failed(session_id, str(exc))

    async def start(self) -> None:
        self.ticking += 1

    async def stop(self) -> None:
        self.stopped += 1


class ApplyCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ManagedStore(Path(self._tmp.name) / "d.db")
        self.service = ManagedService(store=self.store)
        self.player = FakePlayer()
        self.runtime = FakeRuntime(self.service, self.player)
        patcher = mock.patch.object(song_bridge, "build_runtime",
                                    mock.Mock(return_value=self.runtime))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sid = self.service.open_session(target="lx06", transport="rpc",
                                             player_backend="mediaplayer",
                                             principal="songloft")["session_id"]

    def _enqueue(self, sid: str | None = None) -> None:
        from core.music.model import TrackRef

        self.service.queue_add(sid or self.sid,
                               TrackRef.make("kuwo", "42", {}), principal="songloft")

    def _receipt(self, kind: str) -> str:
        return self.service.command(self.sid, kind=kind, principal="songloft",
                                    idem_key=f"{kind}-1", target="lx06").receipt_id

    def _receipt_state(self, receipt_id: str) -> str:
        row = self.store.receipt_by_id(receipt_id)
        assert row is not None
        return str(row["state"])

    async def test_play_binds_session_and_starts_playback(self) -> None:
        self._enqueue()
        rid = self._receipt("play")
        await song_bridge.apply_command(self.sid, "play", rid)
        self.assertEqual(self.runtime.session_id, self.sid)
        self.assertEqual(self.player.started, [self.sid])
        self.assertEqual(self._receipt_state(rid), "observed")

    async def test_play_on_empty_queue_reports_failed(self) -> None:
        rid = self._receipt("play")
        await song_bridge.apply_command(self.sid, "play", rid)
        self.assertEqual(self.player.started, [])
        self.assertEqual(self._receipt_state(rid), "failed")

    async def test_device_rejection_is_failed_not_observed(self) -> None:
        self.player.fail_start = True
        self._enqueue()
        rid = self._receipt("play")
        await song_bridge.apply_command(self.sid, "play", rid)
        self.assertEqual(self._receipt_state(rid), "failed")

    async def test_pause_requires_bound_session(self) -> None:
        rid = self._receipt("pause")
        await song_bridge.apply_command(self.sid, "pause", rid)
        self.assertEqual(self.player.paused, 0)
        self.assertEqual(self._receipt_state(rid), "failed")

    async def test_next_restarts_current_entry(self) -> None:
        self._enqueue()
        self._enqueue()
        self.runtime.session_id = self.sid
        rid = self._receipt("next")
        await song_bridge.apply_command(self.sid, "next", rid)
        self.assertEqual(self.player.started, [self.sid])
        self.assertEqual(self._receipt_state(rid), "observed")

    async def test_stop_halts_ticks_and_unbinds(self) -> None:
        self.runtime.session_id = self.sid
        rid = self._receipt("stop")
        await song_bridge.apply_command(self.sid, "stop", rid)
        self.assertEqual(self.runtime.stopped, 1)
        self.assertIsNone(self.runtime.session_id)
        self.assertEqual(self._receipt_state(rid), "observed")

    async def test_stop_during_slow_play_leaves_no_ghost_session(self) -> None:
        # play 的解析/准备可能几十秒；期间的 stop 必须串行等待，不能出现
        # 「stop 先解绑、play 后 start」的幽灵会话（设备在响但运行期未绑定）。
        self._enqueue()
        gate = asyncio.Event()
        original_start = self.player.start

        async def slow_start(session_id: str, **kwargs):
            await gate.wait()
            return await original_start(session_id, **kwargs)

        self.player.start = slow_start  # type: ignore[assignment]
        rid_play = self._receipt("play")
        play_task = asyncio.ensure_future(song_bridge.apply_command(self.sid, "play", rid_play))
        await asyncio.sleep(0.01)
        rid_stop = self._receipt("stop")
        stop_task = asyncio.ensure_future(song_bridge.apply_command(self.sid, "stop", rid_stop))
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(play_task, stop_task)
        self.assertIsNone(self.runtime.session_id, "stop 之后不得留下已起播但未绑定的会话")
        self.assertEqual("stopped", self.service.snapshot(self.sid)["phase"])

    async def test_switching_session_stops_previous_runtime(self) -> None:
        self._enqueue()
        other = self.service.open_session(target="lx06", transport="rpc",
                                          player_backend="mediaplayer",
                                          principal="other-client")["session_id"]
        self.runtime.session_id = other
        rid = self._receipt("play")
        await song_bridge.apply_command(self.sid, "play", rid)
        self.assertEqual(self.runtime.stopped, 1)
        self.assertEqual(self.runtime.session_id, self.sid)
        # 旧会话必须落 stopped：否则旧客户端一直显示 playing 而设备已被接管
        self.assertEqual(self.service.snapshot(other)["phase"], "stopped")
        self.assertNotEqual(self.service.snapshot(self.sid)["phase"], "stopped")


class ScheduleDeviceTest(unittest.IsolatedAsyncioTestCase):
    def _api(self):
        from core.music.auth import MusicAuth
        from core.services.managed_api import ManagedAPI

        svc = mock.Mock()
        api = ManagedAPI(svc, MusicAuth(""))
        return api

    async def test_schedule_runs_executor(self) -> None:
        api = self._api()
        seen: list[tuple] = []

        async def executor(sid, kind, rid):
            seen.append((sid, kind, rid))

        api.set_device_executor(executor)
        api._schedule_device("ms-1", "play", "r-1")
        await asyncio.sleep(0)
        self.assertEqual(seen, [("ms-1", "play", "r-1")])

    async def test_unknown_kind_is_not_scheduled(self) -> None:
        api = self._api()
        seen: list[tuple] = []

        async def executor(sid, kind, rid):
            seen.append((sid, kind, rid))

        api.set_device_executor(executor)
        api._schedule_device("ms-1", "seek", "r-1")
        await asyncio.sleep(0)
        self.assertEqual(seen, [])

    async def test_executor_failure_is_swallowed(self) -> None:
        api = self._api()

        async def executor(sid, kind, rid):
            raise RuntimeError("boom")

        api.set_device_executor(executor)
        api._schedule_device("ms-1", "play", "r-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(api._device_tasks), 0)


class _SilentBackend:
    profile = RPC

    def __init__(self) -> None:
        self.played: list[str] = []

    async def play(self, uri: str) -> bool:
        self.played.append(uri)
        return True

    async def stop(self) -> bool:
        return True

    async def state(self) -> str:
        return "playing"


class FirstStartVisibleTest(unittest.IsolatedAsyncioTestCase):
    """E1 覆盖面实测缺陷（2026-10-30）：命令路径首次起播失败必须落成**可见停止**。

    修前 `apply_command` 直接 `await player.start()`，抛错只把回执标 failed，会话永久
    停在 `preparing`——那一刻 runtime 还没 start，没有 tick 来兜底，用户看到
    「没有播放，也没有原因」。推进路径（缺陷 #12）早已是「有界跳过 + 末项
    stopped(stop_reason=advance_failed)」；本用例锁住命令路径与它同口径
    （真 player + 真 store，只有设备与解析器是假的）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ManagedStore(Path(self._tmp.name) / "f.db")
        self.addCleanup(self.store.close)
        self.service = ManagedService(store=self.store)
        self.backend = _SilentBackend()
        self.failing: set[str] = set()

        async def resolve(entry):
            if entry.ref.id in self.failing:
                raise ManagedError("songloft prepare timeout")
            return f"http://media/{entry.ref.id}.mp3"

        self.player = ManagedQueuePlayer(self.store, self.backend, resolve, clock=lambda: 0.0)
        self.runtime = ManagedRuntime(self.player, service=self.service, interval_s=10.0)
        patcher = mock.patch.object(song_bridge, "build_runtime",
                                    mock.Mock(return_value=self.runtime))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sid = self.service.open_session(
            target="lx06", transport="rpc", player_backend="mediaplayer",
            principal="songloft")["session_id"]

    def _enqueue(self, track_id: str) -> None:
        self.service.queue_add(self.sid, TrackRef.make("kuwo", track_id, {}),
                               principal="songloft")

    def _receipt(self, kind: str) -> str:
        return self.service.command(self.sid, kind=kind, principal="songloft",
                                    idem_key=f"{kind}-1", target="lx06").receipt_id

    def _receipt_state(self, receipt_id: str) -> str:
        row = self.store.receipt_by_id(receipt_id)
        assert row is not None
        return str(row["state"])

    async def test_last_entry_failure_stops_visibly_not_preparing(self) -> None:
        self.failing.add("bad")
        self._enqueue("bad")
        rid = self._receipt("play")
        await song_bridge.apply_command(self.sid, "play", rid)
        await self.runtime.stop()

        state = self.store.get(self.sid)
        self.assertIsNot(Phase.PREPARING, state.phase, "会话不得停在 preparing")
        self.assertIs(Phase.STOPPED, state.phase)
        self.assertEqual("advance_failed", state.stop_reason)
        self.assertEqual("failed", self._receipt_state(rid))
        self.assertEqual([], self.backend.played, "解析失败不该向设备下发 URI")

    async def test_failure_skips_to_next_entry_and_observes_receipt(self) -> None:
        self.failing.add("bad")
        self._enqueue("bad")
        self._enqueue("good")
        rid = self._receipt("play")
        await song_bridge.apply_command(self.sid, "play", rid)
        await self.runtime.stop()

        state = self.store.get(self.sid)
        self.assertEqual(1, state.current_index, "失败条应被有界跳过")
        self.assertIs(Phase.PLAYING, state.phase)
        self.assertEqual(["http://media/good.mp3"], self.backend.played)
        self.assertEqual("observed", self._receipt_state(rid))
        events, _more = self.store.events_after(self.sid, 0)
        kinds = [e.get("kind") for e in events]
        self.assertIn("advance_failed", kinds, "跳过必须留事件证据")


if __name__ == "__main__":
    unittest.main()
