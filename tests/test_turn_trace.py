"""TurnTrace（ticket 11）契约测试。

全部纯内存、stdlib unittest：不连真机、不联网、不触 Rust 扩展。

覆盖：

  1. 开关：env 1/true/yes/on 关 0/false/no/off，非法值回落 config，默认关闭；
  2. 字段：各段 ms、长度、工具轮数、timeout/fallback 标志，缺失段为 -1；
  3. 格式化：单行 `[TurnTrace]`、key=value 全数值；
  4. 关闭时零输出、零当前轮；开启时每轮恰好一条 INFO；
  5. 无文本/PII：ASR 原文与回复文本都不进入埋点与日志行；
  6. 播放适配器 `_finalize_session` 收尾：把 started/finished 落进当前轮；
  7. `logger.ai_response` 的 module 前缀路由（OpenAI/QwenPaw 不再静默）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.device.playback_events import EventPlaneConfig, PlaybackEventAdapter  # noqa: E402
from core.utils import turn_trace  # noqa: E402
from core.utils.logger import logger as app_logger  # noqa: E402
from core.utils.turn_trace import (  # noqa: E402
    ENV_TURN_TRACE_ENABLED,
    TurnTrace,
    format_turn_trace,
    trace_enabled,
)
from fake_device import CommandResult, FakeDevice, FakeXiaoAI, load_speaker_module  # noqa: E402

speaker_module = load_speaker_module()

from core import ref  # noqa: E402
from core.device import playback_events  # noqa: E402

PREFIX = "[TurnTrace] "
TTS_PATH = "/tmp/tts/tts_turn_trace_test.mp3"


def synth_stdout() -> str:
    info = json.dumps({"path": TTS_PATH}, ensure_ascii=False)
    return json.dumps({"code": 0, "info": info}, ensure_ascii=False)


def context_stdout(status: int = 0) -> str:
    info = json.dumps({"status": status, "volume": 39}, ensure_ascii=False)
    return json.dumps({"code": 0, "info": info}, ensure_ascii=False)


def parse_line(line: str) -> dict[str, str]:
    assert line.startswith(PREFIX), line
    fields: dict[str, str] = {}
    for token in line[len(PREFIX) :].split(" "):
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


class _SequenceReader:
    """按调用次序返回状态；最后一项重复（模拟轮询）。"""

    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    async def __call__(self):
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


class FlagTest(unittest.TestCase):
    def test_env_values(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: value}):
                self.assertTrue(trace_enabled(lambda: False), value)
        for value in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: value}):
                self.assertFalse(trace_enabled(lambda: True), value)

    def test_invalid_env_falls_back_to_config(self):
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "maybe"}):
            self.assertTrue(trace_enabled(lambda: True))
            self.assertFalse(trace_enabled(lambda: False))

    def test_default_off_without_env(self):
        env = {k: v for k, v in os.environ.items() if k != ENV_TURN_TRACE_ENABLED}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(trace_enabled(lambda: False))
            self.assertTrue(trace_enabled(lambda: True))


class SummaryTest(unittest.TestCase):
    def test_segments_and_flags(self):
        trace = TurnTrace(7, source="xiaoai_asr", asr_chars=8, started_at=1000.0)
        trace.mark("llm_start", 1000.010)
        trace.mark("llm_end", 1000.310)
        trace.note_reply_chars(42)
        trace.note_tool_rounds(2)
        trace.mark("tts_start", 1000.320)
        trace.mark("tts_end", 1000.820)
        trace.note_tts_chars(44)
        trace.note_tts_result(ok=True)
        trace.mark("play_url", 1000.825)
        trace.note_play_url(True)
        trace.note_playback(
            finished=True,
            started=True,
            reason="idle",
            wait_started_at=1000.825,
            started_at_ms=20.0,
            finished_at_ms=2020.0,
        )
        summary = trace.summary()
        self.assertEqual(summary["turn"], 7)
        self.assertEqual(summary["src"], "xiaoai_asr")
        self.assertEqual(summary["asr_chars"], 8)
        self.assertEqual(summary["llm_chars"], 42)
        self.assertEqual(summary["tts_chars"], 44)
        self.assertEqual(summary["tool_rounds"], 2)
        self.assertAlmostEqual(summary["asr_to_llm_ms"], 10.0)
        self.assertAlmostEqual(summary["llm_ms"], 300.0)
        self.assertAlmostEqual(summary["llm_to_tts_ms"], 10.0)
        self.assertAlmostEqual(summary["tts_ms"], 500.0)
        self.assertAlmostEqual(summary["tts_to_play_ms"], 5.0)
        self.assertAlmostEqual(summary["accept_to_start_ms"], 20.0)
        self.assertAlmostEqual(summary["playback_ms"], 2000.0)
        self.assertAlmostEqual(summary["total_ms"], 2845.0)
        self.assertEqual(summary["playback_started"], 1)
        self.assertEqual(summary["playback_finished"], 1)
        self.assertEqual(summary["play_url_ok"], 1)
        self.assertEqual(summary["tts_ok"], 1)

    def test_missing_segments_are_minus_one(self):
        trace = TurnTrace(1, source="xiaoai_asr", asr_chars=0, started_at=5.0)
        summary = trace.summary(incomplete=True)
        self.assertEqual(summary["incomplete"], 1)
        self.assertEqual(summary["asr_chars"], 0)
        self.assertEqual(summary["llm_chars"], -1)
        self.assertEqual(summary["llm_ms"], -1.0)
        self.assertEqual(summary["tts_ms"], -1.0)
        self.assertEqual(summary["playback_ms"], -1.0)
        self.assertEqual(summary["play_url_ok"], -1)
        self.assertEqual(summary["playback_finished"], -1)
        self.assertEqual(summary["total_ms"], 0.0)

    def test_format_is_single_numeric_line(self):
        trace = TurnTrace(3, source="xiaoai_asr", asr_chars=2, started_at=1.0)
        trace.mark("llm_start", 1.100)
        trace.mark("llm_end", 1.600)
        line = format_turn_trace(trace.summary())
        self.assertTrue(line.startswith(PREFIX + "turn=3 src=xiaoai_asr"))
        self.assertNotIn("\n", line)
        fields = parse_line(line)
        for key, value in fields.items():
            if key == "src":
                continue
            float(value)  # 全数值（ms 带一位小数，标志/长度是整数）


class SwitchTest(unittest.TestCase):
    def setUp(self):
        turn_trace.reset_turn_trace()

    def tearDown(self):
        turn_trace.reset_turn_trace()

    def test_disabled_emits_nothing(self):
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "0"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                self.assertIsNone(turn_trace.begin_turn("xiaoai_asr", asr_chars=4))
                turn_trace.mark_llm_start()
                turn_trace.mark_llm_end()
                turn_trace.note_reply_chars(10)
                turn_trace.mark_tts_start()
                turn_trace.mark_tts_end()
                turn_trace.note_playback(
                    finished=True,
                    started=True,
                    reason="idle",
                    wait_started_at=time.monotonic(),
                    started_at_ms=0.0,
                    finished_at_ms=10.0,
                )
                self.assertIsNone(turn_trace.current_trace())
                info.assert_not_called()

    def test_enabled_emits_one_line_per_turn(self):
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=6)
                turn_trace.mark_llm_start()
                turn_trace.mark_llm_end()
                turn_trace.note_reply_chars(12)
                turn_trace.note_tool_rounds(1)
                turn_trace.mark_tts_start()
                turn_trace.mark_tts_end()
                turn_trace.note_tts_result(ok=True)
                turn_trace.note_play_url(True)
                wait_start = time.monotonic()
                turn_trace.note_playback(
                    finished=True,
                    started=True,
                    reason="idle",
                    wait_started_at=wait_start,
                    started_at_ms=0.0,
                    finished_at_ms=100.0,
                )
                info.assert_called_once()
                line = info.call_args.args[0]
                fields = parse_line(line)
                self.assertEqual(fields["src"], "xiaoai_asr")
                self.assertEqual(fields["incomplete"], "0")
                self.assertEqual(fields["asr_chars"], "6")
                self.assertEqual(fields["llm_chars"], "12")
                self.assertEqual(fields["tool_rounds"], "1")
                self.assertEqual(fields["playback_started"], "1")
                self.assertEqual(fields["playback_finished"], "1")
                self.assertNotEqual(fields["playback_ms"], "-1.0")
                self.assertIsNone(turn_trace.current_trace())

    def test_unfinished_turn_is_flushed_as_incomplete(self):
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=2)
                turn_trace.begin_turn("xiaoai_asr", asr_chars=3)
                info.assert_called_once()
                fields = parse_line(info.call_args.args[0])
                self.assertEqual(fields["incomplete"], "1")
                self.assertEqual(fields["asr_chars"], "2")
                self.assertIsNotNone(turn_trace.current_trace())

    def test_abandon_turn_flushes_incomplete(self):
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=2)
                turn_trace.abandon_turn()
                info.assert_called_once()
                fields = parse_line(info.call_args.args[0])
                self.assertEqual(fields["incomplete"], "1")
                self.assertEqual(fields["llm_ms"], "-1.0")
                self.assertIsNone(turn_trace.current_trace())


class NoTextTest(unittest.TestCase):
    SECRET = "打开客厅空调到二十六度"

    def setUp(self):
        turn_trace.reset_turn_trace()

    def tearDown(self):
        turn_trace.reset_turn_trace()

    def test_line_contains_lengths_not_text(self):
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=len(self.SECRET))
                turn_trace.mark_llm_start()
                turn_trace.mark_llm_end()
                turn_trace.note_reply_chars(len(self.SECRET) + 2)
                turn_trace.mark_tts_start()
                turn_trace.mark_tts_end()
                turn_trace.note_tts_chars(len(self.SECRET) + 2)
                turn_trace.note_playback(
                    finished=True,
                    started=False,
                    reason="idle_grace",
                    wait_started_at=time.monotonic(),
                    started_at_ms=None,
                    finished_at_ms=None,
                )
                line = info.call_args.args[0]
                self.assertNotIn(self.SECRET, line)
                self.assertNotIn("空调", line)
                self.assertIn(f"asr_chars={len(self.SECRET)}", line)
                self.assertIn(f"tts_chars={len(self.SECRET) + 2}", line)


class PlaybackAdapterHookTest(unittest.TestCase):
    def setUp(self):
        turn_trace.reset_turn_trace()

    def tearDown(self):
        turn_trace.reset_turn_trace()

    def make_adapter(self, values=(1, 0)) -> PlaybackEventAdapter:
        return PlaybackEventAdapter(
            context_reader=_SequenceReader(values),
            config=EventPlaneConfig(
                source="context",
                shadow_sources=(),
                poll_interval_ms=5.0,
                start_grace_ms=50.0,
            ),
        )

    def test_finalize_closes_traced_turn(self):
        adapter = self.make_adapter((1, 0))
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=3)
                turn_trace.mark_tts_start()
                turn_trace.mark_tts_end()
                asyncio.run(adapter.wait_for_finish(300))
                info.assert_called_once()
                fields = parse_line(info.call_args.args[0])
                self.assertEqual(fields["playback_started"], "1")
                self.assertEqual(fields["playback_finished"], "1")
                self.assertNotEqual(fields["playback_ms"], "-1.0")
                self.assertIsNone(turn_trace.current_trace())

    def test_finalize_ignores_untraced_playback(self):
        adapter = self.make_adapter((1, 0))
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                with mock.patch.object(turn_trace.logger, "debug"):
                    asyncio.run(adapter.wait_for_finish(300))
                info.assert_not_called()

    def test_finalize_does_not_close_turn_without_tts(self):
        adapter = self.make_adapter((1, 0))
        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=3)
                turn_trace.mark_llm_start()
                asyncio.run(adapter.wait_for_finish(300))
                info.assert_not_called()
                self.assertIsNotNone(turn_trace.current_trace())


class SpeakerTraceHookTest(unittest.TestCase):
    """`_play_text_blocking` 埋点：TTS 合成前后 + play_url 接受。"""

    def setUp(self):
        turn_trace.reset_turn_trace()
        self.device = FakeDevice()
        ref.GLOBAL_STATES.clear()
        ref.set_xiaoai(FakeXiaoAI(self.device))
        self.speaker = speaker_module.SpeakerManager()
        playback_events.reset_playback_events()
        self.adapter = playback_events.get_playback_events()
        self._saved_event_plane = self.adapter.configure(
            poll_interval_ms=2, start_grace_ms=10
        )

    def tearDown(self):
        self.adapter.config = self._saved_event_plane
        playback_events.reset_playback_events()
        turn_trace.reset_turn_trace()

    def test_blocking_speech_records_tts_and_playback(self):
        self.device.on("text_to_speech", synth_stdout())
        self.device.on("[ -f", "yes")
        self.device.on("player_play_url", '{"code":0}')
        self.device.on_sequence(
            "player_get_context",
            [
                CommandResult(context_stdout(0)),
                CommandResult(context_stdout(1)),
                CommandResult(context_stdout(0)),
            ],
        )

        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=2)
                ok = asyncio.run(
                    self.speaker.play(text="你好", blocking=True, timeout=500)
                )

        self.assertTrue(ok)
        info.assert_called_once()
        fields = parse_line(info.call_args.args[0])
        self.assertEqual(fields["tts_chars"], "2")
        self.assertEqual(fields["tts_ok"], "1")
        self.assertEqual(fields["tts_fallback"], "0")
        self.assertEqual(fields["play_url_ok"], "1")
        self.assertEqual(fields["playback_started"], "1")
        self.assertNotEqual(fields["tts_ms"], "-1.0")
        self.assertNotEqual(fields["playback_ms"], "-1.0")
        self.assertIsNone(turn_trace.current_trace())

    def test_synthesis_fallback_is_flagged(self):
        self.device.on("text_to_speech", '{"code":1}')
        self.device.on("tts_play.sh", "", exit_code=1)
        self.device.on("player_get_context", context_stdout(0))

        with mock.patch.dict(os.environ, {ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch.object(turn_trace.logger, "info") as info:
                turn_trace.begin_turn("xiaoai_asr", asr_chars=2)
                ok = asyncio.run(
                    self.speaker.play(text="你好", blocking=True, timeout=500)
                )

        self.assertTrue(ok)
        info.assert_called_once()
        fields = parse_line(info.call_args.args[0])
        self.assertEqual(fields["tts_ok"], "0")
        self.assertEqual(fields["tts_fallback"], "1")
        self.assertEqual(fields["play_url_ok"], "-1")


class LoggerRoutingTest(unittest.TestCase):
    def test_openai_prefix_is_routed(self):
        with mock.patch.object(app_logger.logger, "info") as info:
            app_logger.ai_response("好的", module="OpenAI(agent:default:home)")
            info.assert_called_once()
            message = info.call_args.args[0]
            self.assertIn("OpenAI", message)
            self.assertIn("好的", message)

    def test_qwenpaw_prefix_is_routed(self):
        with mock.patch.object(app_logger.logger, "info") as info:
            app_logger.ai_response("好的", module="QwenPaw(agent:default:home)")
            info.assert_called_once()
            self.assertIn("QwenPaw", info.call_args.args[0])

    def test_unknown_prefix_stays_silent(self):
        with mock.patch.object(app_logger.logger, "info") as info:
            app_logger.ai_response("好的", module="Other")
            info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
