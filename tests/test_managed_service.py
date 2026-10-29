"""托管播放命令门面 + 事件总线 + HTTP/SSE（B-05）契约测试。

纯内存/临时 SQLite + aiohttp TestServer：不连设备、不联网。覆盖：

  1. 幂等回执：同 principal+key+payload 返回原回执；异 payload 409。
  2. 代次闸门：stale expected_epoch 拒绝；四个入口看到同一 session/epoch。
  3. 状态与关键事件同一事务写入；事件带迁移后的 epoch 与单调 seq。
  4. 回放：Last-Event-ID 续、保留窗口缺口 ⇒ gap/reset；慢订阅者 ⇒ reset 标记。
  5. HTTP/SSE：鉴权失败关闭、target 越权 403、命令 202/200、SSE 事件帧。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from core.music import events as events_module  # noqa: E402
from core.music import store as store_module  # noqa: E402
from core.music.auth import MusicAuth, MusicAuthError  # noqa: E402
from core.music.events import EventBus  # noqa: E402
from core.music.model import Intent, ManagedError, Phase, StaleEpoch, TrackRef  # noqa: E402
from core.music.service import ConflictError, ManagedService  # noqa: E402
from core.music.store import ManagedStore  # noqa: E402
from core.services.managed_api import ManagedAPI  # noqa: E402

TOKENS = json.dumps({
    "tok-app": {"name": "songloft", "targets": ["lx06"]},
    "tok-mobile": {"name": "mobile", "targets": ["lx06", "lx06-kitchen"]},
    "tok-read": {"name": "viewer", "targets": ["lx06"], "scopes": ["read", "stream"]},
})


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ManagedStore(Path(self._tmp.name) / "state.db")
        self.bus = EventBus(self.store, max_queue=8)
        self.service = ManagedService(store=self.store, bus=self.bus)
        self.session = self.service.open_session(target="lx06", principal="songloft")
        self.sid = self.session["session_id"]

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _cmd(self, kind, *, key, payload=None, principal="songloft",
             epoch=None, target="lx06"):
        return self.service.command(
            self.sid, kind=kind, payload=payload, principal=principal,
            idem_key=key, expected_epoch=epoch, target=target,
        )


class CommandReceiptTest(_Base):
    def test_idempotent_replay_returns_the_original_receipt(self) -> None:
        first = self._cmd("pause", key="k1")
        again = self._cmd("pause", key="k1")
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        self.assertEqual(first.receipt_id, again.receipt_id)
        self.assertEqual(first.epoch, again.epoch)

    def test_same_key_different_payload_conflicts(self) -> None:
        self._cmd("stop", key="k2", payload={"reason": "user"})
        with self.assertRaises(ConflictError):
            self._cmd("stop", key="k2", payload={"reason": "ha"})

    def test_receipt_is_scoped_to_principal(self) -> None:
        self._cmd("pause", key="shared")
        other = self._cmd("pause", key="shared", principal="mobile")
        self.assertFalse(other.replayed, "不同主体不得共享幂等回执")

    def test_stale_epoch_is_rejected(self) -> None:
        with self.assertRaises(StaleEpoch):
            self._cmd("pause", key="k3", epoch=999)
        self.assertEqual(Phase.PREPARING, self.store.get(self.sid).phase)

    def test_unknown_command_and_missing_key_are_rejected(self) -> None:
        with self.assertRaises(ManagedError):
            self._cmd("explode", key="k4")
        with self.assertRaises(ManagedError):
            self._cmd("pause", key="")

    def test_target_must_match_the_session(self) -> None:
        from core.music.service import TargetMismatch

        with self.assertRaises(TargetMismatch):
            self._cmd("pause", key="k5", target="lx06-kitchen")


class SharedSessionTest(_Base):
    def test_all_entries_see_one_session_and_epoch(self) -> None:
        # 四种入口（Agent/HTTP/KWS/HA 的抽象）都只是 command()，必须看到同一代次
        for track in ("1", "2", "3", "4"):
            self.store.append(self.sid, TrackRef.make("kuwo", track))
        principals = ["songloft", "mobile", "bridge-kws", "ha"]
        epochs = []
        for i, principal in enumerate(principals):
            receipt = self._cmd("next", key=f"n-{i}", principal=principal)
            epochs.append(receipt.epoch)
            current = self.store.get(self.sid)
            self.assertEqual(current.epoch, receipt.epoch)
        self.assertEqual(sorted(epochs), epochs, "代次必须单调递增")

    def test_stop_clears_recovery_and_advance_then_fences_old_epoch(self) -> None:
        self.store.append(self.sid, TrackRef.make("kuwo", "1"))
        self.store.mark_playing(self.sid)
        self.store.grant_recovery(self.sid, "tok")
        epoch = self.store.get(self.sid).epoch
        receipt = self._cmd("stop", key="stop-1", epoch=epoch)
        state = self.store.get(self.sid)
        self.assertEqual(Intent.STOPPED, state.intent)
        self.assertIsNone(state.recovery_token)
        self.assertFalse(state.auto_advance)
        with self.assertRaises(StaleEpoch):
            self._cmd("pause", key="pause-after-stop", epoch=epoch)
        self.assertEqual(receipt.epoch, state.epoch)


class EventLogTest(_Base):
    def test_state_and_event_share_one_commit(self) -> None:
        receipt = self._cmd("pause", key="ev-1")
        state = self.store.get(self.sid)
        events, gap = self.store.events_after(self.sid, 0)
        self.assertFalse(gap)
        kinds = [e["kind"] for e in events]
        self.assertIn("session_opened", kinds)
        command = [e for e in events if e["kind"] == "command"][-1]
        self.assertEqual(receipt.epoch, command["epoch"])
        self.assertEqual(state.epoch, command["epoch"])
        self.assertEqual(receipt.receipt_id, command["receipt_id"])
        self.assertTrue(all(a["seq"] < b["seq"] for a, b in zip(events, events[1:])),
                        "seq 必须单调")

    def test_replay_after_last_event_id(self) -> None:
        self._cmd("pause", key="a")
        events, _ = self.store.events_after(self.sid, 0)
        last = events[-1]["seq"]
        self._cmd("resume", key="b")
        newer, gap = self.store.events_after(self.sid, last)
        self.assertFalse(gap)
        self.assertEqual(1, len(newer))
        self.assertEqual("resume", newer[0]["command"])

    def test_retention_gap_is_reported(self) -> None:
        original = store_module.EVENT_RETAIN
        store_module.EVENT_RETAIN = 2
        try:
            for i in range(5):
                self._cmd("pause" if i % 2 == 0 else "resume", key=f"r{i}")
            _, gap = self.store.events_after(self.sid, 1)
            self.assertTrue(gap, "早于保留窗口的 Last-Event-ID 必须判缺口")
        finally:
            store_module.EVENT_RETAIN = original

    def test_slow_subscriber_gets_reset_without_blocking(self) -> None:
        bus = EventBus(self.store, max_queue=1)
        sub = bus.subscribe(self.sid)
        for i in range(3):
            bus.publish(self.sid, {"kind": "command", "command": "pause", "seq": i})
        marker = asyncio.run(sub.get(timeout=0.1))
        self.assertEqual("reset", marker["kind"])
        self.assertEqual("slow_consumer", marker["reason"])


class ApiAuthTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ManagedStore(Path(self._tmp.name) / "api.db")
        self.service = ManagedService(store=self.store)
        self.api = ManagedAPI(self.service, MusicAuth(TOKENS))
        app = web.Application()
        self.api.register(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.store.close()
        self._tmp.cleanup()

    async def _create(self, token="tok-app", target="lx06"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = await self.client.post("/api/music/sessions",
                                      json={"target": target}, headers=headers)
        return resp

    async def test_auth_failures_are_closed(self) -> None:
        resp = await self._create(token=None)
        self.assertEqual(401, resp.status)
        resp = await self._create(token="nope")
        self.assertEqual(403, resp.status)
        resp = await self._create(token="tok-app", target="office")
        self.assertEqual(403, resp.status, "target 越权必须 403")

    async def test_unconfigured_auth_is_503(self) -> None:
        api = ManagedAPI(self.service, MusicAuth(""))
        app = web.Application()
        api.register(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/api/music/sessions", json={"target": "lx06"},
                                     headers={"Authorization": "Bearer x"})
            self.assertEqual(503, resp.status)
        finally:
            await client.close()

    async def test_command_receipt_replay_and_conflict(self) -> None:
        created = await self._create()
        self.assertEqual(201, created.status)
        sid = (await created.json())["data"]["session_id"]
        headers = {"Authorization": "Bearer tok-app"}
        body = {"kind": "pause", "idem_key": "k1", "target": "lx06"}

        first = await self.client.post(f"/api/music/sessions/{sid}/commands",
                                       json=body, headers=headers)
        self.assertEqual(202, first.status)
        again = await self.client.post(f"/api/music/sessions/{sid}/commands",
                                       json=body, headers=headers)
        self.assertEqual(200, again.status)
        self.assertTrue((await again.json())["data"]["replayed"])

        conflict = await self.client.post(
            f"/api/music/sessions/{sid}/commands",
            json={**body, "kind": "stop"}, headers=headers)
        self.assertEqual(409, conflict.status)
        self.assertEqual("idempotency_conflict", (await conflict.json())["error"])

    async def test_sse_replays_and_continues(self) -> None:
        created = await self._create()
        sid = (await created.json())["data"]["session_id"]
        headers = {"Authorization": "Bearer tok-app"}
        await self.client.post(f"/api/music/sessions/{sid}/commands",
                               json={"kind": "pause", "idem_key": "s1", "target": "lx06"},
                               headers=headers)

        resp = await self.client.get(
            f"/api/music/sessions/{sid}/events?target=lx06", headers=headers)
        self.assertEqual(200, resp.status)
        try:
            block = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), timeout=3)
            text = block.decode()
            self.assertIn("event: session_opened", text)
            # 续读：第二帧是命令事件
            block = await asyncio.wait_for(resp.content.readuntil(b"\n\n"), timeout=3)
            text = block.decode()
            self.assertIn("event: command", text)
            self.assertIn("id: ", text)
        finally:
            resp.close()

    async def test_sse_rejects_unauthenticated(self) -> None:
        created = await self._create()
        sid = (await created.json())["data"]["session_id"]
        resp = await self.client.get(f"/api/music/sessions/{sid}/events?target=lx06")
        self.assertEqual(401, resp.status)

    async def test_scope_limits_endpoints(self) -> None:
        created = await self._create()
        sid = (await created.json())["data"]["session_id"]
        read_headers = {"Authorization": "Bearer tok-read"}
        ok = await self.client.get(f"/api/music/sessions/{sid}?target=lx06",
                                   headers=read_headers)
        self.assertEqual(200, ok.status)
        denied = await self.client.post(
            f"/api/music/sessions/{sid}/commands",
            json={"kind": "pause", "idem_key": "ro-1", "target": "lx06"},
            headers=read_headers)
        self.assertEqual(403, denied.status, "read-only 凭据不得下发命令")

    async def test_read_path_is_bound_to_the_session_target(self) -> None:
        # 另一个会话属于 lx06-kitchen；lx06 凭据读它必须 403
        created = await self._create(token="tok-mobile", target="lx06-kitchen")
        sid = (await created.json())["data"]["session_id"]
        resp = await self.client.get(f"/api/music/sessions/{sid}?target=lx06",
                                     headers={"Authorization": "Bearer tok-app"})
        self.assertEqual(403, resp.status)
