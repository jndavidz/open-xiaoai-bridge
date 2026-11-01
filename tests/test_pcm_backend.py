"""PcmBackend + 桥侧进度记账（pcm-backend spec 票 03）契约测试。

seam = PlaybackBackend 公共接口（play/stop/pause/resume/state/position/seek）
+ ManagedQueuePlayer 的 resume/seek 编排。native 用 FakeNative（推流不落地，
不连设备、不出声）。核心断言：**PCM 会话不依赖设备 status**——
state()/position() 全部由桥侧编排器记账回答。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music.backends import PcmBackend, PCM  # noqa: E402
from core.music.model import TrackRef  # noqa: E402
from core.music.pcm_orchestrator import PcmOrchestrator  # noqa: E402
from core.music.player import ManagedQueuePlayer, Unsupported  # noqa: E402
from core.music.store import ManagedStore  # noqa: E402
from tests.test_pcm_orchestrator import FakeNative  # noqa: E402


class PcmBackendTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.native = FakeNative()
        self.orch = PcmOrchestrator(native=self.native)
        self.backend = PcmBackend(orchestrator=self.orch)

    async def test_profile_claims_bridge_accounted_capabilities(self) -> None:
        self.assertTrue(PCM.position)
        self.assertTrue(PCM.seek)
        self.assertTrue(PCM.resume)
        self.assertTrue(PCM.stop)

    async def test_play_starts_orchestrator(self) -> None:
        self.assertTrue(await self.backend.play("http://cdn/a.mp3"))
        self.assertEqual(self.native.starts, [("http://cdn/a.mp3", 0)])
        self.assertEqual(await self.backend.state(), "playing")

    async def test_play_failure_reports_false_not_raise(self) -> None:
        self.native.start_error = RuntimeError("rejected")
        self.assertFalse(await self.backend.play("http://cdn/a.mp3"))
        self.assertEqual(await self.backend.state(), "idle")

    async def test_state_answers_from_orchestrator_without_device(self) -> None:
        # PcmBackend 没有 device 通道：PCM 会话必须不碰设备 status
        self.assertFalse(hasattr(self.backend, "device"))
        await self.backend.play("http://cdn/a.mp3")
        self.assertEqual(await self.backend.state(), "playing")
        await self.backend.pause()
        self.assertEqual(await self.backend.state(), "paused")
        await self.backend.stop()
        self.assertEqual(await self.backend.state(), "idle")

    async def test_position_is_bridge_accounted(self) -> None:
        await self.backend.play("http://cdn/a.mp3")
        self.orch.refresh()
        self.assertAlmostEqual(await self.backend.position(), 0.0, places=2)  # 起点如实为 0
        self.native.progress(100, 5_000)
        self.orch.refresh()
        self.assertAlmostEqual(await self.backend.position(), 5.0, places=2)

    async def test_seek_repositions_push(self) -> None:
        await self.backend.play("http://cdn/a.mp3")
        self.native.progress(100, 1_000)
        self.orch.refresh()
        await self.backend.seek(30.0)
        self.assertEqual(self.native.starts[-1], ("http://cdn/a.mp3", 30_000))
        self.assertAlmostEqual(await self.backend.position(), 30.0, places=2)

    async def test_ensure_resumed_does_not_touch_device(self) -> None:
        self.store = ManagedStore(Path(tempfile.mkdtemp()) / "p.db")
        self.addCleanup(self.store.close)
        sid = self.store.open_session(target="lx06").session_id
        self.store.append(sid, TrackRef.make("kuwo", "a"), duration_s=10)
        player = ManagedQueuePlayer(
            self.store, self.backend,
            lambda entry: _resolve(entry), clock=lambda: 0.0,
        )
        await player.start(sid)
        await player.pause(sid)
        # TTS focus 场景：设备状态未知也必须走编排器续推并回读 playing
        outcome = await player.ensure_resumed(sid)
        self.assertEqual(outcome, "resumed")
        self.assertEqual(await self.backend.state(), "playing")
        self.assertEqual(self.native.starts[-1], ("http://cdn/a.mp3", 0))

    async def test_player_seek_delegates_to_pcm_backend(self) -> None:
        self.store = ManagedStore(Path(tempfile.mkdtemp()) / "p.db")
        self.addCleanup(self.store.close)
        sid = self.store.open_session(target="lx06").session_id
        self.store.append(sid, TrackRef.make("kuwo", "a"), duration_s=100)
        player = ManagedQueuePlayer(
            self.store, self.backend,
            lambda entry: _resolve(entry), clock=lambda: 0.0,
        )
        await player.start(sid)
        await player.seek(sid, 30.0)
        self.assertEqual(self.native.starts[-1], ("http://cdn/a.mp3", 30_000))

    async def test_url_backend_still_rejects_seek(self) -> None:
        # 双轨安全：URL backend 的 seek 语义不变（profile 未声明 → 明拒）
        from core.music.backends import RpcBackend

        class _FakeDevice:
            async def play_url(self, uri):
                return type("R", (), {"ok": True})()

        self.store = ManagedStore(Path(tempfile.mkdtemp()) / "p.db")
        self.addCleanup(self.store.close)
        sid = self.store.open_session(target="lx06").session_id
        self.store.append(sid, TrackRef.make("kuwo", "a"), duration_s=100)
        player = ManagedQueuePlayer(
            self.store, RpcBackend(device=_FakeDevice()),
            lambda entry: _resolve(entry), clock=lambda: 0.0,
        )
        await player.start(sid)
        with self.assertRaises(Unsupported):
            await player.seek(sid, 30.0)


async def _resolve(entry) -> str:
    return f"http://cdn/{entry.ref.id}.mp3"


if __name__ == "__main__":
    unittest.main()
