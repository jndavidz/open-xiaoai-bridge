"""托管播放会话/队列/存储 + 单设备执行器（B-04）契约测试。

纯内存/临时 SQLite：不连设备、不联网。覆盖 DoD 四件事：

  1. 同曲重复 entry 独立；旧 epoch 的命令/回写被拒绝。
  2. stop 在内存与持久状态里先作废恢复资格和自动推进；重启只恢复元数据并进入
     recovery_pending（不授予恢复资格）。
  3. 位置快照带 backend/采样时刻/operation/新鲜度；transport 与 player_backend
     两个维度互不顶替。
  4. I/O 走有界单设备执行器；stop 不被阻塞；迟到的 Play/seek 结果进入 UNKNOWN
     并接受一次性补偿 Stop，而不是重发。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music import (  # noqa: E402
    CommandState,
    CommandTicket,
    Intent,
    ManagedError,
    ManagedExecutor,
    ManagedStore,
    Phase,
    PositionSnapshot,
    QueueFull,
    SessionNotFound,
    StaleEpoch,
    TrackRef,
)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "managed.db"
        self.store = ManagedStore(self.db)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _session(self, **kwargs):
        return self.store.open_session(**kwargs)


class QueueIdentityTest(_Base):
    async def test_same_track_twice_gets_independent_entries(self) -> None:
        s = self._session()
        ref = TrackRef.make("kuwo", "228908")
        a = self.store.append(s.session_id, ref)
        b = self.store.append(s.session_id, ref)
        self.assertNotEqual(a.entry_id, b.entry_id)
        self.assertEqual(2, len(self.store.get(s.session_id).entries))

    async def test_stale_epoch_is_rejected(self) -> None:
        s = self._session()
        epoch = self.store.bump_epoch(s.session_id)
        with self.assertRaises(StaleEpoch):
            self.store.append(s.session_id, TrackRef.make("qq", "a1"), epoch=epoch - 1)
        with self.assertRaises(StaleEpoch):
            self.store.pause(s.session_id, epoch=epoch - 1)
        self.assertEqual([], self.store.get(s.session_id).entries)


class StopAndRecoveryTest(_Base):
    async def test_stop_invalidates_recovery_and_advance_in_one_go(self) -> None:
        s = self._session()
        self.store.append(s.session_id, TrackRef.make("kuwo", "1"))
        state = self.store.mark_playing(s.session_id)
        state.auto_advance = True
        self.store._save(state)
        self.store.grant_recovery(s.session_id, "tok-1")

        stopped = self.store.stop(s.session_id)
        self.assertEqual(Intent.STOPPED, stopped.intent)
        self.assertIsNone(stopped.recovery_token)
        self.assertFalse(stopped.auto_advance)

        # 持久化口径：换一个连接读同一文件，仍是 STOPPED 且无恢复资格
        other = ManagedStore(self.db)
        try:
            reloaded = other.get(s.session_id)
            self.assertEqual(Phase.STOPPED, reloaded.phase)
            self.assertIsNone(reloaded.recovery_token)
            self.assertFalse(reloaded.auto_advance)
        finally:
            other.close()

    async def test_stopped_session_cannot_grant_recovery(self) -> None:
        s = self._session()
        self.store.stop(s.session_id)
        with self.assertRaises(ManagedError):
            self.store.grant_recovery(s.session_id, "tok-2")

    async def test_restore_only_metadata_and_recovery_pending(self) -> None:
        s = self._session()
        self.store.append(s.session_id, TrackRef.make("migu", "600902",
                                                      {"copyrightId": "C1"}))
        self.store.mark_playing(s.session_id)
        self.store.grant_recovery(s.session_id, "tok-3")

        other = ManagedStore(self.db)
        try:
            restored = other.restore()
            self.assertEqual(1, len(restored))
            state = restored[0]
            self.assertEqual(Phase.RECOVERY_PENDING, state.phase)
            self.assertIsNone(state.recovery_token)
            self.assertFalse(state.auto_advance)
            # 元数据（身份）保留
            self.assertEqual("600902", state.entries[0].ref.id)
            self.assertEqual({"copyrightId": "C1"}, state.entries[0].ref.extra_dict)
        finally:
            other.close()

    async def test_missing_session_is_explicit(self) -> None:
        self.assertIsNone(self.store.get("nope"))
        with self.assertRaises(SessionNotFound):
            self.store.append("nope", TrackRef.make("qq", "x"))


class PositionSnapshotTest(_Base):
    async def test_snapshot_carries_backend_time_operation_and_freshness(self) -> None:
        s = self._session(transport="dlna", player_backend="mediaplayer")
        snap = PositionSnapshot(position_s=42.0, sampled_at=time.time(),
                                operation="poll", fresh_for_s=5.0,
                                transport="dlna", player_backend="mediaplayer")
        self.store.set_position(s.session_id, snap)
        stored = self.store.get(s.session_id).position
        self.assertTrue(stored.is_fresh())
        self.assertFalse(stored.is_fresh(now=stored.sampled_at + 60))

    async def test_transport_and_backend_are_not_interchangeable(self) -> None:
        s = self._session(transport="dlna", player_backend="mediaplayer")
        wrong_backend = PositionSnapshot(
            position_s=1.0, sampled_at=time.time(), operation="poll",
            fresh_for_s=5.0, transport="dlna", player_backend="miplayer")
        with self.assertRaises(ManagedError):
            self.store.set_position(s.session_id, wrong_backend)
        wrong_transport = PositionSnapshot(
            position_s=1.0, sampled_at=time.time(), operation="poll",
            fresh_for_s=5.0, transport="rpc", player_backend="mediaplayer")
        with self.assertRaises(ManagedError):
            self.store.set_position(s.session_id, wrong_transport)


class ExecutorTest(_Base):
    def _executor(self, **kwargs) -> ManagedExecutor:
        def is_current(ticket: CommandTicket) -> bool:
            try:
                state = self.store.get(ticket.session_id)
            except SessionNotFound:
                return False
            return state.epoch == ticket.epoch and state.intent != Intent.STOPPED

        return ManagedExecutor(is_current=is_current, **kwargs)

    async def test_stop_is_not_blocked_and_late_result_is_unknown(self) -> None:
        s = self._session()
        compensated: list[str] = []

        async def compensate(ticket):
            compensated.append(ticket.command_id)

        executor = self._executor(compensate=compensate)
        release = asyncio.Event()
        calls = {"n": 0}

        async def slow_play():
            calls["n"] += 1
            await release.wait()
            return True

        await executor.start()
        try:
            task = asyncio.ensure_future(
                executor.submit(CommandTicket(s.session_id, s.epoch, "play"), slow_play))
            await asyncio.sleep(0.01)

            # stop 只改 store 代次，立即返回；不排队、不被阻塞
            self.store.stop(s.session_id)
            self.assertFalse(task.done())

            release.set()
            state = await asyncio.wait_for(task, timeout=2)
        finally:
            await executor.close()

        self.assertEqual(CommandState.UNKNOWN, state)
        self.assertEqual(1, calls["n"], "迟到命令不得重发")
        self.assertEqual(1, len(compensated), "迟到结果必须收到一次补偿 Stop")

    async def test_queued_command_from_old_epoch_never_executes(self) -> None:
        s = self._session()
        executor = self._executor()
        release = asyncio.Event()
        calls = {"n": 0}

        async def slow_play():
            calls["n"] += 1
            await release.wait()
            return True

        await executor.start()
        try:
            first = asyncio.ensure_future(
                executor.submit(CommandTicket(s.session_id, s.epoch, "play"), slow_play))
            await asyncio.sleep(0.01)
            self.store.stop(s.session_id)   # 后一条仍带旧 epoch
            second = asyncio.ensure_future(
                executor.submit(CommandTicket(s.session_id, s.epoch, "seek"), slow_play))
            release.set()
            await asyncio.wait_for(first, timeout=2)
            state = await asyncio.wait_for(second, timeout=2)
        finally:
            await executor.close()

        self.assertEqual(CommandState.UNKNOWN, state)
        self.assertEqual(1, calls["n"], "旧代次命令不得执行")

    async def test_bounded_queue_rejects_instead_of_growing(self) -> None:
        s = self._session()
        executor = self._executor(max_pending=1)
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return True

        await executor.start()
        try:
            first = asyncio.ensure_future(
                executor.submit(CommandTicket(s.session_id, s.epoch, "play"), slow))
            await asyncio.sleep(0.01)   # 让第一条出队并占住 worker
            waiting = asyncio.ensure_future(
                executor.submit(CommandTicket(s.session_id, s.epoch, "seek"), slow))
            await asyncio.sleep(0.01)
            with self.assertRaises(QueueFull):
                await executor.submit(
                    CommandTicket(s.session_id, s.epoch, "seek2"), slow)
            release.set()
            await asyncio.wait_for(first, timeout=2)
            await asyncio.wait_for(waiting, timeout=2)
        finally:
            await executor.close()


if __name__ == "__main__":
    unittest.main()
