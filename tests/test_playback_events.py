"""播放完成事件适配器（ticket 05）的契约测试。

全部纯内存、stdlib unittest：不连真机、不联网、不触 Rust 扩展。
覆盖三种事件源、影子比对与「等待不被影子源阻塞」：

  1. context：1→0 成对；从未见 1、宽限后空闲；读不到状态；超时兜底。
  2. client：playing→idle；paused 不算结束；重复/乱序事件；无事件即无法确认。
  3. native：`play_status_broadcast_event` 行解析（合法 / 噪声 / 非 JSON）。
  4. 影子：不一致计数；一致时零计数；慢影子源不阻塞主等待。
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.device.playback_events import (  # noqa: E402
    EventPlaneConfig,
    PlaybackEventAdapter,
    parse_native_play_status,
)


def _run(coro):
    return asyncio.run(coro)


class SequenceReader:
    """按调用次序返回状态；最后一项重复（模拟轮询）。"""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    async def __call__(self):
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        value = self.values[index]
        if isinstance(value, Exception):
            raise value
        return value


class SlowReader:
    """影子 context 源：故意慢到远超主源判定时间。"""

    def __init__(self, seconds: float = 10.0):
        self.seconds = seconds
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return 1


def make_adapter(
    *,
    source: str = "context",
    shadows=(),
    reader=None,
    poll: float = 5.0,
    grace: float = 20.0,
    native_cmd: str = "",
) -> PlaybackEventAdapter:
    return PlaybackEventAdapter(
        context_reader=reader if reader is not None else SequenceReader([None]),
        config=EventPlaneConfig(
            source=source,
            shadow_sources=tuple(shadows),
            poll_interval_ms=poll,
            start_grace_ms=grace,
            native_cmd=native_cmd,
        ),
    )


class ContextSourceTest(unittest.TestCase):
    def test_playing_then_idle_is_finished(self) -> None:
        adapter = make_adapter(reader=SequenceReader([1, 1, 0]))

        started = time.monotonic()
        result = _run(adapter.wait_for_finish(2000, 100))
        elapsed = time.monotonic() - started

        self.assertEqual(result, (True, True, "idle"))
        self.assertLess(elapsed, 1.0)

    def test_never_playing_but_idle_finishes_after_grace(self) -> None:
        adapter = make_adapter(reader=SequenceReader([0]), grace=40)

        started = time.monotonic()
        result = _run(adapter.wait_for_finish(2000, 40))
        elapsed_ms = (time.monotonic() - started) * 1000

        self.assertEqual(result, (True, False, "idle_grace"))
        self.assertGreaterEqual(elapsed_ms, 30.0, "必须等宽限期，不能一看到空闲就返回")
        self.assertLess(elapsed_ms, 1000.0)

    def test_unreadable_status_cannot_confirm_completion(self) -> None:
        adapter = make_adapter(reader=SequenceReader([None]))

        result = _run(adapter.wait_for_finish(80, 10))

        self.assertEqual(result, (False, False, "no_observation"))

    def test_reader_exception_is_treated_as_no_observation(self) -> None:
        adapter = make_adapter(reader=SequenceReader([RuntimeError("rpc broken")]))

        result = _run(adapter.wait_for_finish(80, 10))

        self.assertEqual(result, (False, False, "no_observation"))

    def test_started_without_finish_uses_timeout_fallback(self) -> None:
        adapter = make_adapter(reader=SequenceReader([1]))

        started = time.monotonic()
        result = _run(adapter.wait_for_finish(120, 10))
        elapsed = time.monotonic() - started

        self.assertEqual(result, (True, True, "timeout"))
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 1.0)

    def test_idle_only_but_budget_shorter_than_grace_is_timeout(self) -> None:
        adapter = make_adapter(reader=SequenceReader([0]), grace=5000)

        result = _run(adapter.wait_for_finish(80, 5000))

        self.assertEqual(result, (True, False, "timeout"))


class ClientSourceTest(unittest.TestCase):
    def test_playing_then_idle_finishes(self) -> None:
        adapter = make_adapter(source="client", grace=100)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(2000))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.02)
            adapter.feed_client_status("idle")
            return await task

        self.assertEqual(_run(scenario()), (True, True, "idle"))

    def test_paused_is_not_finished(self) -> None:
        adapter = make_adapter(source="client", grace=100)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(160))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.01)
            adapter.feed_client_status("paused")
            await asyncio.sleep(0.02)
            adapter.feed_client_status("paused")
            return await task

        started = time.monotonic()
        result = _run(scenario())
        elapsed = time.monotonic() - started

        self.assertEqual(result, (True, True, "timeout"))
        self.assertGreaterEqual(elapsed, 0.15, "paused 不得提前解锁")
        self.assertNotEqual(result[2], "idle")

    def test_paused_only_counts_as_started(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(80))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("paused")
            return await task

        self.assertEqual(_run(scenario()), (True, True, "timeout"))

    def test_duplicate_and_out_of_order_events(self) -> None:
        adapter = make_adapter(source="client", grace=100)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(2000))
            await asyncio.sleep(0.005)
            adapter.feed_client_status("idle")  # 乱序：先到 idle
            await asyncio.sleep(0.005)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.005)
            adapter.feed_client_status("idle")
            adapter.feed_client_status("idle")  # 重复
            return await task

        result = _run(scenario())

        self.assertEqual(result, (True, True, "idle"))

    def test_playing_before_wait_is_entry_state(self) -> None:
        adapter = make_adapter(source="client", grace=100)

        async def scenario():
            adapter.feed_client_status("playing")
            task = asyncio.ensure_future(adapter.wait_for_finish(500))
            await asyncio.sleep(0.02)
            adapter.feed_client_status("idle")
            return await task

        self.assertEqual(_run(scenario()), (True, True, "idle"))

    def test_no_events_at_all_cannot_confirm(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        result = _run(adapter.wait_for_finish(60))

        self.assertEqual(result, (False, False, "no_observation"))

    def test_unknown_client_payload_is_ignored(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(80))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("buffering")
            adapter.feed_client_status("")
            return await task

        self.assertEqual(_run(scenario()), (False, False, "no_observation"))


class NativeLineParseTest(unittest.TestCase):
    def test_valid_status_lines(self) -> None:
        self.assertEqual(
            parse_native_play_status(
                '01:23:04 { "play_status_broadcast_event": {"status":1} }'
            ),
            1,
        )
        self.assertEqual(
            parse_native_play_status('{"play_status_broadcast_event":{"status":0}}'),
            0,
        )
        # 兼容转发层/监控层的 data 包装形态。
        self.assertEqual(
            parse_native_play_status(
                '{"method":"play_status_broadcast_event","data":{"status":1}}'
            ),
            1,
        )
        self.assertEqual(parse_native_play_status('{"data":{"status":0}}'), 0)

    def test_noise_lines_are_ignored(self) -> None:
        for line in (
            "",
            "   ",
            "led show {\"L\":3}",
            "ubus listen output",
            '{"other_event": {"status": 1}}',
            '{"play_status_broadcast_event": {"status": 2}}',
            '{"play_status_broadcast_event": {"status": true}}',
            '{"play_status_broadcast_event": {"status": "1"}}',
            '{"play_status_broadcast_event": "status"}',
            "[1, 2, 3]",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_native_play_status(line))

    def test_non_json_after_brace_is_ignored(self) -> None:
        self.assertIsNone(parse_native_play_status('12:00:00 { "status":1'))

    def test_adapter_ignores_noise_and_accepts_status(self) -> None:
        adapter = make_adapter(source="native", grace=10)

        adapter.feed_native_line("led show {\"L\":3}")
        adapter.feed_native_line("garbage")
        self.assertNotIn("native", adapter.snapshot()["sources"])
        self.assertEqual(adapter.snapshot()["event_counts"]["native"], 0)

        adapter.feed_native_line('{"play_status_broadcast_event": {"status": 1}}')
        snapshot = adapter.snapshot()
        self.assertEqual(snapshot["sources"]["native"]["kind"], "playing")
        self.assertEqual(snapshot["event_counts"]["native"], 1)


class NativeSourceTest(unittest.TestCase):
    def test_native_status_pair_finishes(self) -> None:
        adapter = make_adapter(source="native", grace=100)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(2000))
            await asyncio.sleep(0.01)
            adapter.feed_native_line(
                '01:23:04 { "play_status_broadcast_event": {"status":1} }'
            )
            await asyncio.sleep(0.02)
            adapter.feed_native_line(
                '01:23:08 { "play_status_broadcast_event": {"status":0} }'
            )
            return await task

        self.assertEqual(_run(scenario()), (True, True, "finished_event"))

    def test_native_without_cmd_does_not_start_stream(self) -> None:
        adapter = make_adapter(source="native", grace=10)

        result = _run(adapter.wait_for_finish(60))
        snapshot = adapter.snapshot()

        self.assertEqual(result, (False, False, "no_observation"))
        self.assertFalse(snapshot["native_cmd_configured"])
        self.assertFalse(snapshot["native_running"])


class ShadowModeTest(unittest.TestCase):
    def test_mismatch_is_counted_without_changing_result(self) -> None:
        adapter = make_adapter(
            source="context",
            shadows=["client"],
            reader=SequenceReader([1, 0]),
        )

        result = _run(adapter.wait_for_finish(2000, 50))
        snapshot = adapter.snapshot()

        self.assertEqual(result, (True, True, "idle"))
        # client 影子源什么都没看到 ⇒ 开始标志不一致（且没来得及看到结束）
        self.assertGreaterEqual(snapshot["mismatch_count"], 1)
        self.assertIsNotNone(snapshot["last_mismatch"])
        kinds = snapshot["last_mismatch"]["mismatches"][0]["kinds"]
        self.assertIn("started_flag", kinds)
        self.assertEqual(snapshot["last_result"]["source"], "context")
        self.assertIn("client", snapshot["last_result"]["timelines"])

    def test_consistent_sources_leave_zero_mismatch(self) -> None:
        adapter = make_adapter(
            source="context",
            shadows=["client"],
            reader=SequenceReader([1, 1, 1, 0]),
            poll=5,
            grace=50,
        )

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(2000, 50))
            await asyncio.sleep(0.002)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.01)
            adapter.feed_client_status("idle")
            return await task

        result = _run(scenario())
        snapshot = adapter.snapshot()

        self.assertEqual(result[0], True)
        self.assertEqual(snapshot["mismatch_count"], 0)
        client_timeline = snapshot["last_result"]["timelines"]["client"]
        self.assertTrue(client_timeline["started"])
        self.assertTrue(client_timeline["finished"])
        self.assertEqual(client_timeline["role"], "shadow")

    def test_slow_shadow_source_does_not_block_wait(self) -> None:
        adapter = make_adapter(
            source="client",
            shadows=["context"],
            reader=SlowReader(10.0),
        )

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(3000))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.02)
            adapter.feed_client_status("idle")
            return await task

        started = time.monotonic()
        result = _run(scenario())
        elapsed = time.monotonic() - started

        self.assertEqual(result, (True, True, "idle"))
        self.assertLess(elapsed, 1.0, "影子源是慢速轮询时不得阻塞主等待")


class SnapshotAndConfigTest(unittest.TestCase):
    def test_snapshot_exposes_source_shadow_events_and_counters(self) -> None:
        adapter = make_adapter(
            source="context",
            shadows=["client"],
            reader=SequenceReader([0]),
            grace=10,
        )

        _run(adapter.wait_for_finish(500, 10))
        snapshot = adapter.snapshot()

        self.assertEqual(snapshot["source"], "context")
        self.assertEqual(snapshot["shadow_sources"], ["client"])
        self.assertEqual(snapshot["waits"], 1)
        self.assertIn("mismatch_count", snapshot)
        self.assertIn("recent_events", snapshot)
        self.assertTrue(
            any(item["source"] == "context" for item in snapshot["recent_events"]),
            snapshot["recent_events"],
        )
        self.assertEqual(snapshot["last_result"]["reason"], "idle_grace")

    def test_config_defaults(self) -> None:
        config = EventPlaneConfig.from_mapping({})

        self.assertEqual(config.source, "context")
        self.assertEqual(config.shadow_sources, ("client",))
        self.assertEqual(config.poll_interval_ms, 200.0)
        self.assertEqual(config.start_grace_ms, 1000.0)
        self.assertEqual(config.native_cmd, "")

    def test_invalid_source_falls_back_to_context(self) -> None:
        config = EventPlaneConfig.from_mapping(
            {"source": "bogus", "shadow_sources": ["nope", "client", "client"]}
        )

        self.assertEqual(config.source, "context")
        self.assertEqual(config.shadow_sources, ("client",))

    def test_configure_returns_previous_for_restore(self) -> None:
        adapter = make_adapter(reader=SequenceReader([0]))
        previous = adapter.configure(poll_interval_ms=7)

        self.assertEqual(previous.poll_interval_ms, 5.0)
        self.assertEqual(adapter.config.poll_interval_ms, 7.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
