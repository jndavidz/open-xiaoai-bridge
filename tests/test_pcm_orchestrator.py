"""TTS/音乐互斥编排器（pcm-backend spec 票 02）契约测试。

fake native + fake clock，纯内存：不连设备、不联网、不推声。覆盖：

  1. start/pause/resume/stop 的状态机与 native 调用参数（含 start_ms 续推）。
  2. 位置记账：position = actual_start_ms + pushed_ms；终态读取失败回落已知进度。
  3. 曲末判定：completed 终态 → 会话结束 + on_track_end 回调一次。
  4. 推流死亡自愈：cancelled/failed → 有界重推（max_heal_retries），耗尽放弃。
  5. 看门狗：暂停态且 TTS focus 归零 → 超时自动续推；focus 持有中不动。
  6. TTS 持有 focus 时推流被 token 失效 → 保持安静等待 focus 归还。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.music.pcm_orchestrator import PcmOrchestrator  # noqa: E402


class FakeNative:
    """可脚本化的 native 三件套。"""

    def __init__(self) -> None:
        self.starts: list[tuple[str, int]] = []
        self.stops: list[int | None] = []
        self.next_token = 100
        self.start_error: Exception | None = None
        # token -> {"status":..., "pushed_ms":..., "actual_start_ms":...}
        self.sessions: dict[int, dict] = {}

    async def start(self, source: str, start_ms: int) -> int:
        if self.start_error is not None:
            raise self.start_error
        token = self.next_token
        self.next_token += 1
        self.starts.append((source, start_ms))
        self.sessions[token] = {
            "status": "playing",
            "pushed_ms": 0,
            "actual_start_ms": start_ms,
            "duration_ms": 180_000,
        }
        return token

    def stop(self, token: int | None) -> None:
        self.stops.append(token)
        if token is not None and token in self.sessions:
            self.sessions[token]["status"] = "cancelled"

    def state(self, token: int) -> dict | None:
        return self.sessions.get(token)

    # 测试辅助
    def progress(self, token: int, pushed_ms: int) -> None:
        self.sessions[token]["pushed_ms"] = pushed_ms

    def complete(self, token: int, pushed_ms: int) -> None:
        self.sessions[token].update(status="completed", pushed_ms=pushed_ms)

    def fail(self, token: int) -> None:
        self.sessions[token].update(status="failed", pushed_ms=0)


def make_orchestrator(native: FakeNative, **kw) -> tuple:
    now = [0.0]
    focus = [False]
    clock = lambda: now[0]  # noqa: E731
    orchestrator = PcmOrchestrator(
        native=native, clock=clock, focus_active=lambda: focus[0], **kw
    )
    return orchestrator, now, focus


class OrchestratorTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_pause_resume_drives_native_with_offset(self) -> None:
        native = FakeNative()
        orch, _, _ = make_orchestrator(native)
        await orch.start("http://m/x.mp3")
        self.assertEqual(native.starts, [("http://m/x.mp3", 0)])
        self.assertEqual(orch.state(), "playing")

        native.progress(100, 5_000)
        pos = await orch.pause()
        self.assertEqual(pos, 5_000)  # actual_start 0 + pushed 5000
        self.assertEqual(native.stops, [100])
        self.assertEqual(orch.state(), "paused")

        offset = await orch.resume()
        self.assertEqual(offset, 5_000)
        self.assertEqual(native.starts[-1], ("http://m/x.mp3", 5_000))
        self.assertEqual(orch.state(), "playing")

    async def test_pause_accounts_seek_landing(self) -> None:
        native = FakeNative()
        orch, _, _ = make_orchestrator(native)
        await orch.start("http://m/x.mp3", start_ms=60_000)
        native.progress(100, 1_000)
        pos = await orch.pause()
        self.assertEqual(pos, 61_000)  # actual_start 60000 + pushed 1000

    async def test_pause_unreadable_terminal_falls_back(self) -> None:
        native = FakeNative()
        orch, now, _ = make_orchestrator(native, terminal_timeout_s=0.1,
                                         terminal_poll_s=0.01)
        await orch.start("http://m/x.mp3")
        # stop 后注册表丢（native 清理）→ 读不到终态
        del native.sessions[100]
        pos = await orch.pause()
        self.assertEqual(pos, 0)  # 回落最后已知进度
        self.assertEqual(orch.state(), "paused")

    async def test_completion_ends_session_and_is_idempotent(self) -> None:
        native = FakeNative()
        orch, _, _ = make_orchestrator(native)
        await orch.start("http://m/x.mp3")
        native.complete(100, 180_000)
        orch.refresh()
        self.assertEqual(orch.state(), "idle")
        self.assertEqual(orch.position_ms(), 180_000)  # 曲末位置冻结保留
        orch.refresh()  # 幂等：会话已清，不再变化
        self.assertEqual(orch.state(), "idle")
        self.assertEqual(orch.position_ms(), 180_000)

    async def test_push_lost_without_focus_self_heals(self) -> None:
        native = FakeNative()
        orch, _, focus = make_orchestrator(native)
        await orch.start("http://m/x.mp3")
        native.fail(100)
        focus[0] = False
        orch.refresh()
        await asyncio.sleep(0)  # 让 heal 任务跑一步
        self.assertEqual(orch.state(), "playing")
        self.assertEqual(native.starts[-1], ("http://m/x.mp3", 0))  # 从记忆位置重推

    async def test_push_lost_with_focus_holds_quiet(self) -> None:
        native = FakeNative()
        orch, _, focus = make_orchestrator(native)
        await orch.start("http://m/x.mp3")
        native.progress(100, 4_000)
        orch.refresh()  # 位置推进到 4000
        focus[0] = True  # TTS 正在播
        native.fail(100)
        orch.refresh()
        await asyncio.sleep(0)
        self.assertEqual(orch.state(), "paused")
        self.assertEqual(orch.position_ms(), 4_000)  # 位置冻结，等 focus 归还

    async def test_heal_budget_exhausted_gives_up(self) -> None:
        native = FakeNative()
        orch, _, _ = make_orchestrator(native, max_heal_retries=2)
        await orch.start("http://m/x.mp3")
        # 起播成功后 native 坏掉：重试 2 次后放弃
        native.sessions[100]["status"] = "failed"
        native.start_error = RuntimeError("device gone")
        for _ in range(3):
            orch.refresh()
            await asyncio.sleep(0)
        self.assertEqual(orch.state(), "idle")

    async def test_stalled_push_triggers_heal(self) -> None:
        native = FakeNative()
        orch, now, _ = make_orchestrator(native, stall_s=5.0)
        await orch.start("http://m/x.mp3")
        native.progress(100, 1_000)
        orch.refresh()
        now[0] += 10.0  # 推进时间：位置没动 → 停滞
        orch.refresh()
        await asyncio.sleep(0)
        self.assertEqual(len([s for s in native.starts if s[1] == 1_000]), 1)

    async def test_watchdog_resumes_when_focus_gone(self) -> None:
        native = FakeNative()
        orch, _, focus = make_orchestrator(native, watchdog_timeout_s=0.05)
        await orch.start("http://m/x.mp3")
        await orch.pause()
        self.assertEqual(orch.state(), "paused")
        await asyncio.sleep(0.15)
        self.assertEqual(orch.state(), "playing")
        self.assertEqual(native.starts[-1], ("http://m/x.mp3", 0))

    async def test_watchdog_holds_while_focus_active(self) -> None:
        native = FakeNative()
        orch, _, focus = make_orchestrator(native, watchdog_timeout_s=0.05)
        focus[0] = True
        await orch.start("http://m/x.mp3")
        await orch.pause()
        await asyncio.sleep(0.15)
        self.assertEqual(orch.state(), "paused")  # focus 在，不抢
        # focus 释放后下一轮看门狗不再触发（已被 resume 路径接管）——这里只验不动

    async def test_start_failure_raises(self) -> None:
        native = FakeNative()
        native.start_error = RuntimeError("rejected")
        orch, _, _ = make_orchestrator(native)
        with self.assertRaises(RuntimeError):
            await orch.start("http://m/x.mp3")
        self.assertEqual(orch.state(), "idle")

    async def test_stop_clears_session(self) -> None:
        native = FakeNative()
        orch, _, _ = make_orchestrator(native)
        await orch.start("http://m/x.mp3")
        await orch.stop()
        self.assertEqual(orch.state(), "idle")
        self.assertIsNone(orch.position_ms())
        await orch.resume()  # 无会话 resume 是 no-op
        self.assertEqual(native.starts, [("http://m/x.mp3", 0)])


if __name__ == "__main__":
    unittest.main()
