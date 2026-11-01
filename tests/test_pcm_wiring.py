"""命令/路由接线与双轨兼容（pcm-backend spec 票 04）契约测试。

seam = song_bridge.build_runtime 的 backend 选源（config `playback_backend`，
默认 url 双轨冻结）+ ManagedRuntime tick 的桥侧位置馈送（曲末判定）。
不连设备、不出声。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music import song_bridge  # noqa: E402
from core.music.backends import PcmBackend, RpcBackend  # noqa: E402
from core.music.model import TrackRef  # noqa: E402
from core.music.pcm_orchestrator import PcmOrchestrator  # noqa: E402
from core.music.player import Decision, ManagedQueuePlayer  # noqa: E402
from core.music.runtime import ManagedRuntime, set_runtime  # noqa: E402
from core.music.store import ManagedStore  # noqa: E402
from tests.test_pcm_orchestrator import FakeNative  # noqa: E402


class PlaybackBackendSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        song_bridge._reset_for_tests()
        self.addCleanup(song_bridge._reset_for_tests)
        self.addCleanup(set_runtime, None)

    def _factory(self):
        return lambda *a, **k: ManagedStore(Path(self._tmp.name) / "t.db")

    def _build(self, cfg: dict | None = None):
        patches = [mock.patch.object(song_bridge, "ManagedStore", self._factory())]
        if cfg is not None:
            patches.append(mock.patch.object(
                song_bridge._sr, "_cfg",
                lambda key, default=None: cfg.get(key, default)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        runtime = song_bridge.build_runtime()
        self.addCleanup(runtime.player.store.close)  # Windows：先关 db 再清临时目录
        return runtime

    def test_default_backend_is_url_rpc(self) -> None:
        self.assertIsInstance(self._build().player.backend, RpcBackend)

    def test_pcm_backend_selected_by_config(self) -> None:
        self.assertIsInstance(
            self._build({"playback_backend": "pcm"}).player.backend, PcmBackend)

    def test_unknown_backend_value_falls_back_to_url(self) -> None:
        self.assertIsInstance(
            self._build({"playback_backend": "carrier-pigeon"}).player.backend,
            RpcBackend)


class BridgeSideEndOfTrackTest(unittest.IsolatedAsyncioTestCase):
    """曲末判定：pcm 会话由桥侧推流完成事件驱动推进，不等墙钟。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ManagedStore(Path(self._tmp.name) / "p.db")
        from core.music.service import ManagedService

        self.service = ManagedService(store=self.store)
        self.native = FakeNative()
        self.orch = PcmOrchestrator(native=self.native)
        self.backend = PcmBackend(orchestrator=self.orch)
        self.now = [0.0]
        self.sid = self.store.open_session(target="lx06", transport="rpc",
                                           player_backend="mediaplayer").session_id

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    async def test_completed_push_advances_before_wall_clock(self) -> None:
        self.store.append(self.sid, TrackRef.make("kuwo", "a"), duration_s=10)
        self.store.append(self.sid, TrackRef.make("kuwo", "b"), duration_s=10)

        async def resolve(entry):
            return f"http://cdn/{entry.ref.id}.mp3"

        player = ManagedQueuePlayer(self.store, self.backend, resolve,
                                    clock=lambda: self.now[0])
        runtime = ManagedRuntime(player)
        runtime.bind(self.sid)
        await runtime.start_current(self.sid)
        self.assertEqual([s[0] for s in self.native.starts], ["http://cdn/a.mp3"])

        # 整曲推完（桥侧终态），墙钟才 1 秒：位置馈送必须让 EOF 判定到点
        self.native.complete(100, 10_000)
        self.now[0] = 1.0
        decision = await runtime.tick_once()
        self.assertIs(Decision.ADVANCE, decision)
        self.assertEqual(self.native.starts[-1], ("http://cdn/b.mp3", 0))

    async def test_native_duration_backfills_unknown_duration_entries(self) -> None:
        # HTTP/App 入队无 duration_s：pcm 模式下靠 native 流头时长补缺，
        # 否则 EOF 判定永远 unknown、播完不推进（spec 验收 3）。
        self.store.append(self.sid, TrackRef.make("kuwo", "a"))  # 无 duration_s
        self.store.append(self.sid, TrackRef.make("kuwo", "b"), duration_s=10)

        async def resolve(entry):
            return f"http://cdn/{entry.ref.id}.mp3"

        player = ManagedQueuePlayer(self.store, self.backend, resolve,
                                    clock=lambda: self.now[0])
        runtime = ManagedRuntime(player)
        runtime.bind(self.sid)
        await runtime.start_current(self.sid)
        self.native.sessions[100]["duration_ms"] = 9_000  # native 流头解析出 9s
        self.native.complete(100, 9_000)  # 整曲推完
        self.now[0] = 1.0
        decision = await runtime.tick_once()
        self.assertIs(Decision.ADVANCE, decision)

    async def test_position_feed_publishes_to_session_snapshot(self) -> None:
        # 桥侧记账位置必须进会话快照（App/SSE 可观测），否则 /api/music 快照
        # position 恒 None（2026-11-02 真机验收 P3 暴露）。
        self.store.append(self.sid, TrackRef.make("kuwo", "a"), duration_s=100)

        async def resolve(entry):
            return f"http://cdn/{entry.ref.id}.mp3"

        player = ManagedQueuePlayer(self.store, self.backend, resolve,
                                    clock=lambda: self.now[0])
        runtime = ManagedRuntime(player, service=self.service)
        runtime.bind(self.sid)
        await runtime.start_current(self.sid)
        self.native.progress(100, 5_000)
        self.now[0] = 1.0
        await runtime.tick_once()
        snap = runtime.service.snapshot(self.sid)
        pos = snap.get("position")
        self.assertIsNotNone(pos, "position must be published to the snapshot")
        self.assertAlmostEqual(pos["position_s"], 5.0, places=2)
        self.assertEqual(pos["operation"], "pcm-accounted")
        self.assertEqual(pos["transport"], "rpc")

    async def test_seek_command_unlocked_for_pcm(self) -> None:
        # B-06 时代 service.command 对 seek 硬拒；pcm backend 落地后必须放行
        # （票 03/04 桥侧重投），否则 HTTP seek 一直 400。
        from core.music.service import ManagedService

        self.service2 = ManagedService(store=self.store)
        self.store.append(self.sid, TrackRef.make("kuwo", "a"), duration_s=200)

        async def resolve(entry):
            return f"http://cdn/{entry.ref.id}.mp3"

        player = ManagedQueuePlayer(self.store, self.backend, resolve,
                                    clock=lambda: self.now[0])
        runtime = ManagedRuntime(player, service=self.service2)
        runtime.bind(self.sid)
        await runtime.start_current(self.sid)
        receipt = self.service2.command(
            self.sid, kind="seek", payload={"position_s": 150.0},
            principal="acceptance", idem_key="seek-1", target="lx06",
        )
        self.assertEqual(receipt.state, "accepted")
        # 设备执行侧走 player.seek（桥侧重投）
        await runtime.player.seek(self.sid, 150.0)
        self.assertEqual(self.native.starts[-1], ("http://cdn/a.mp3", 150_000))

    async def test_url_backend_tick_has_no_position_feed(self) -> None:
        # 双轨：url backend 无 position 能力，tick 不额外查询（行为冻结）
        calls: list[int] = []

        class _UrlBackend(RpcBackend):
            async def position(self):
                calls.append(1)
                return None

        self.store.append(self.sid, TrackRef.make("kuwo", "a"), duration_s=10)

        class _FakeDevice:
            async def play_url(self, uri):
                return type("R", (), {"ok": True})()

        backend = _UrlBackend(device=_FakeDevice())
        player = ManagedQueuePlayer(self.store, backend,
                                    lambda entry: _async_url(entry),
                                    clock=lambda: self.now[0])
        runtime = ManagedRuntime(player)
        runtime.bind(self.sid)
        await runtime.start_current(self.sid)
        self.now[0] = 1.0
        await runtime.tick_once()
        self.assertEqual(calls, [])


async def _async_url(entry) -> str:
    return f"http://cdn/{entry.ref.id}.mp3"


if __name__ == "__main__":
    unittest.main()
