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
        self.native = FakeNative()
        self.orch = PcmOrchestrator(native=self.native)
        self.backend = PcmBackend(orchestrator=self.orch)
        self.now = [0.0]
        self.sid = self.store.open_session(target="lx06").session_id

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
