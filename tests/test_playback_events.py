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
from unittest import mock

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


def _triple(result):
    """Completion 的旧三元组视图（断言只看这三项）。"""
    return (result.finished, result.started, result.reason)


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

        self.assertEqual(_triple(result), (True, True, "idle"))
        self.assertLess(elapsed, 1.0)

    def test_never_playing_but_idle_finishes_after_grace(self) -> None:
        adapter = make_adapter(reader=SequenceReader([0]), grace=40)

        started = time.monotonic()
        result = _run(adapter.wait_for_finish(2000, 40))
        elapsed_ms = (time.monotonic() - started) * 1000

        self.assertEqual(_triple(result), (True, False, "idle_grace"))
        self.assertGreaterEqual(elapsed_ms, 30.0, "必须等宽限期，不能一看到空闲就返回")
        self.assertLess(elapsed_ms, 1000.0)

    def test_unreadable_status_cannot_confirm_completion(self) -> None:
        adapter = make_adapter(reader=SequenceReader([None]))

        result = _run(adapter.wait_for_finish(80, 10))

        self.assertEqual(_triple(result), (False, False, "no_observation"))

    def test_reader_exception_is_treated_as_no_observation(self) -> None:
        adapter = make_adapter(reader=SequenceReader([RuntimeError("rpc broken")]))

        result = _run(adapter.wait_for_finish(80, 10))

        self.assertEqual(_triple(result), (False, False, "no_observation"))

    def test_started_without_finish_uses_timeout_fallback(self) -> None:
        adapter = make_adapter(reader=SequenceReader([1]))

        started = time.monotonic()
        result = _run(adapter.wait_for_finish(120, 10))
        elapsed = time.monotonic() - started

        self.assertEqual(_triple(result), (True, True, "timeout"))
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertLess(elapsed, 1.0)

    def test_idle_only_but_budget_shorter_than_grace_is_timeout(self) -> None:
        adapter = make_adapter(reader=SequenceReader([0]), grace=5000)

        result = _run(adapter.wait_for_finish(80, 5000))

        self.assertEqual(_triple(result), (True, False, "timeout"))


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

        self.assertEqual(_triple(_run(scenario())), (True, True, "idle"))

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

        self.assertEqual(_triple(result), (True, True, "timeout"))
        self.assertGreaterEqual(elapsed, 0.15, "paused 不得提前解锁")
        self.assertNotEqual(result[2], "idle")

    def test_paused_only_counts_as_started(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(80))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("paused")
            return await task

        self.assertEqual(_triple(_run(scenario())), (True, True, "timeout"))

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

        self.assertEqual(_triple(result), (True, True, "idle"))

    def test_playing_before_wait_is_entry_state(self) -> None:
        adapter = make_adapter(source="client", grace=100)

        async def scenario():
            adapter.feed_client_status("playing")
            task = asyncio.ensure_future(adapter.wait_for_finish(500))
            await asyncio.sleep(0.02)
            adapter.feed_client_status("idle")
            return await task

        self.assertEqual(_triple(_run(scenario())), (True, True, "idle"))

    def test_no_events_at_all_cannot_confirm(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        result = _run(adapter.wait_for_finish(60))

        self.assertEqual(_triple(result), (False, False, "no_observation"))

    def test_unknown_client_payload_is_ignored(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(80))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("buffering")
            adapter.feed_client_status("")
            return await task

        self.assertEqual(_triple(_run(scenario())), (False, False, "no_observation"))


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

        self.assertEqual(_triple(_run(scenario())), (True, True, "finished_event"))

    def test_native_without_cmd_does_not_start_stream(self) -> None:
        adapter = make_adapter(source="native", grace=10)

        result = _run(adapter.wait_for_finish(60))
        snapshot = adapter.snapshot()

        self.assertEqual(_triple(result), (False, False, "no_observation"))
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

        self.assertEqual(_triple(result), (True, True, "idle"))
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

        self.assertEqual(_triple(result), (True, True, "idle"))
        self.assertLess(elapsed, 1.0, "影子源是慢速轮询时不得阻塞主等待")


class GenerationBindingTest(unittest.TestCase):
    """B-03：起播序号之前的证据不能完成本次播放。"""

    def test_entry_playing_is_ignored_when_since_seq_given(self) -> None:
        adapter = make_adapter(source="client", grace=10)
        adapter.feed_client_status("playing")          # 上一段播放的开播事件
        seq = adapter.begin_playback()

        async def scenario():
            task = asyncio.ensure_future(
                adapter.wait_for_finish(80, since_seq=seq)
            )
            await asyncio.sleep(0.01)
            adapter.feed_client_status("idle")          # 新事件，但本次从未起播
            return await task

        result = _run(scenario())
        self.assertEqual(_triple(result), (True, False, "idle_grace"))
        self.assertEqual(result.quality, "weak")

    def test_events_after_begin_playback_still_complete(self) -> None:
        adapter = make_adapter(source="client", grace=10)
        adapter.feed_client_status("playing")          # 旧段
        seq = adapter.begin_playback()

        async def scenario():
            task = asyncio.ensure_future(
                adapter.wait_for_finish(200, since_seq=seq)
            )
            await asyncio.sleep(0.01)
            adapter.feed_client_status("playing")       # 本段真正起播
            await asyncio.sleep(0.01)
            adapter.feed_client_status("idle")
            return await task

        result = _run(scenario())
        self.assertEqual(_triple(result), (True, True, "idle"))
        self.assertEqual(result.source, "client")
        # 主源（client）亲自看到起播+结束 ⇒ strong；只有影子/宽限兜底才是 weak
        self.assertEqual(result.quality, "strong")

    def test_entry_paused_does_not_count_as_started(self) -> None:
        """旧 paused 不能给「等待开始时已在播放」背书（B-03）。"""

        adapter = make_adapter(source="client", grace=10)
        adapter.feed_client_status("paused")

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(80))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("idle")
            return await task

        result = _run(scenario())
        self.assertEqual(_triple(result), (True, False, "idle_grace"))
        self.assertEqual(result.quality, "weak")


class _EofStream:
    async def readline(self) -> bytes:
        return b""


class PausedEvidenceTest(unittest.TestCase):
    """B-03：暂停不是结束；暂停后未恢复的 idle 不能当作播完。"""

    def test_paused_mid_wait_keeps_paused_flag(self) -> None:
        adapter = make_adapter(source="client", grace=10)

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(200))
            await asyncio.sleep(0.01)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.01)
            adapter.feed_client_status("paused")
            await asyncio.sleep(0.01)
            adapter.feed_client_status("idle")
            return await task

        result = _run(scenario())
        self.assertEqual(_triple(result), (True, True, "idle"))
        self.assertTrue(result.paused, "暂停后未恢复必须留痕")

    def test_context_status_2_is_paused_not_idle(self) -> None:
        adapter = make_adapter(
            source="context", reader=SequenceReader([2]), grace=10
        )
        result = _run(adapter.wait_for_finish(60))
        # 2 曾被当成空闲 ⇒ 宽限后假完成；现在按暂停处理，只能走超时兜底
        self.assertEqual(_triple(result), (True, True, "timeout"))
        self.assertTrue(result.paused)


class _BlockingStream:
    """永不 EOF 的假 stdout（流保持存活）。"""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _EofProcess:
    """立即 EOF 的假子进程（模拟 cmd 不存在 / 无行退出）。"""

    def __init__(self) -> None:
        self.stdout = _EofStream()
        self.returncode = None

    def terminate(self) -> None:
        self.returncode = 0


class NativeBackoffTest(unittest.TestCase):
    """B-03：native 无行退出后按退避，重复 prepare 不得形成 fork 风暴。"""

    def test_repeated_prepare_spawns_once_per_backoff_window(self) -> None:
        adapter = make_adapter(source="native", native_cmd="false")
        spawns: list[str] = []

        async def fake_spawn(cmd, **kwargs):
            spawns.append(str(cmd))
            return _EofProcess()

        async def scenario():
            with mock.patch("asyncio.create_subprocess_shell", fake_spawn):
                first = await adapter.prepare()
                await asyncio.sleep(0.02)   # 让 reader 跑完并登记退避
                second = await adapter.prepare()
                third = await adapter.prepare()
            return first, second, third

        first, second, third = _run(scenario())
        self.assertFalse(second)
        self.assertFalse(third)
        self.assertEqual(1, len(spawns), spawns)
        # 假进程立即 EOF：就绪确认必须如实返回 False（不能只看「fork 成功」）
        self.assertFalse(first)
        self.assertGreaterEqual(adapter.snapshot()["native_attempts"], 1)

    def test_reconnect_happens_after_the_backoff_expires(self) -> None:
        adapter = make_adapter(source="native", native_cmd="false")
        spawns: list[str] = []

        async def fake_spawn(cmd, **kwargs):
            spawns.append(str(cmd))
            return _EofProcess()

        async def scenario():
            with mock.patch("asyncio.create_subprocess_shell", fake_spawn):
                await adapter.prepare()
                await asyncio.sleep(0.02)
                adapter._native_retry_at = 0.0   # 模拟退避窗口已过
                adapter._native_force_at = 0.0
                second = await adapter.prepare()
            return second

        _run(scenario())
        self.assertEqual(2, len(spawns), "退避到期后必须允许重连")

    def test_prepare_reports_ready_for_a_live_stream(self) -> None:
        adapter = make_adapter(source="native", native_cmd="cat")
        spawns: list[str] = []

        class _LiveProcess:
            def __init__(self) -> None:
                self.stdout = _BlockingStream()
                self.returncode = None

            def terminate(self) -> None:
                self.returncode = 0

        async def fake_spawn(cmd, **kwargs):
            spawns.append(str(cmd))
            return _LiveProcess()

        async def scenario():
            with mock.patch("asyncio.create_subprocess_shell", fake_spawn):
                return await adapter.prepare()

        self.assertTrue(_run(scenario()))
        self.assertEqual(1, len(spawns))


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


class ShadowFallbackTest(unittest.TestCase):
    """primary 完全无观测时，影子源的完整 start→idle 不应被判失败。"""

    def test_shadow_finishes_when_primary_no_observation(self) -> None:
        adapter = make_adapter(
            source="context",
            shadows=["client"],
            reader=SequenceReader([None]),
        )

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(400))
            await asyncio.sleep(0.02)
            adapter.feed_client_status("playing")
            await asyncio.sleep(0.02)
            adapter.feed_client_status("idle")
            return await task

        self.assertEqual(_triple(_run(scenario())), (True, True, "shadow_finished"))
        last = adapter.snapshot()["last_result"]
        self.assertEqual(last["reason"], "shadow_finished")
        self.assertEqual(last["completion_source"], "client")
        self.assertEqual(last["completion_quality"], "weak")

    def test_shadow_without_start_does_not_finish(self) -> None:
        adapter = make_adapter(
            source="context",
            shadows=["client"],
            reader=SequenceReader([None]),
        )

        async def scenario():
            task = asyncio.ensure_future(adapter.wait_for_finish(120))
            await asyncio.sleep(0.02)
            adapter.feed_client_status("idle")
            return await task

        self.assertEqual(_triple(_run(scenario())), (False, False, "no_observation"))


class NativeStallWatchdogTest(unittest.TestCase):
    """整个等待期间 native 零状态行 ⇒ 订阅被判僵尸并重启（不退避）。"""

    def test_stalled_stream_is_restarted_after_timeout(self) -> None:
        adapter = make_adapter(source="native", native_cmd="cat", poll=5)
        spawns: list[str] = []

        class _SilentProcess:
            def __init__(self) -> None:
                self.stdout = _BlockingStream()
                self.returncode = None
                self.cancelled = False

            def terminate(self) -> None:
                self.returncode = 0

        procs: list[_SilentProcess] = []

        async def fake_spawn(cmd, **kwargs):
            proc = _SilentProcess()
            procs.append(proc)
            spawns.append(str(cmd))
            return proc

        async def scenario():
            with mock.patch("asyncio.create_subprocess_shell", fake_spawn):
                first = await adapter.wait_for_finish(60, 10)
                await asyncio.sleep(0.02)   # 让 cancelled reader 收尾
                second = await adapter.wait_for_finish(60, 10)
                await asyncio.sleep(0.02)
            return first, second

        first, second = _run(scenario())
        self.assertEqual(_triple(first), (False, False, "no_observation"))
        self.assertEqual(_triple(second), (False, False, "no_observation"))
        self.assertEqual(2, len(spawns), "stalled stream must respawn on next wait")
        self.assertEqual(0.0, adapter._native_retry_at, "watchdog restart must skip backoff")
        self.assertEqual(0, adapter.snapshot()["native_status_lines"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
