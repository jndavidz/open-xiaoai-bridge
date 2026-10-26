"""LLM 流式通道（P1 ticket 03）契约测试——纯内存、录制 SSE chunk 序列。

不连真实上游：aiohttp.ClientSession 以 fake 替身注入（行流可控、可注入
异常），断言 stream_response_segments 的句段产出、降级面、历史与埋点。

覆盖：
  1. SSE 行解析（data JSON / [DONE] / 注释与坏行跳过）；
  2. 增量组装 → 句段：分片跨边界、空 delta（role/reasoning 帧）、
     [DONE] 后残余行、异常中断（部分段有效 + 不入历史 + 埋点收尾）；
  3. 降级面：开关关（默认）/ 非 chat_completions / 工具轮 → 基类单句序列；
  4. 基类默认实现：非流式后端单句序列、空回复无段；
  5. payload 口径：stream=True 覆盖 extra_body 的显式 stream；
  6. 会话历史：流式轮终答全文入历史（与非流式同口径）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for _path in (ROOT, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if "open_xiaoai_server" not in sys.modules:
    sys.modules["open_xiaoai_server"] = types.ModuleType("open_xiaoai_server")
sys.modules.setdefault("sherpa_onnx", types.ModuleType("sherpa_onnx"))

from core.backend_base import BackendManagerBase  # noqa: E402
from core.openai import OpenAIManager, _SSE_DONE, parse_sse_line  # noqa: E402
from core.utils import turn_trace  # noqa: E402


def sse(delta_content: str | None = None, **delta_fields) -> bytes:
    """构造一行 chat_completions SSE data 事件（delta 可省略 content）。"""
    delta: dict = dict(delta_fields)
    if delta_content is not None:
        delta["content"] = delta_content
    event = {"choices": [{"delta": delta}]}
    return f"data: {json.dumps(event, ensure_ascii=False)}\n".encode("utf-8")


SSE_DONE_LINE = b"data: [DONE]\n"


# ---- aiohttp fake ----


class FakeContent:
    """按行投喂的 response.content 替身；lines 中可混入 Exception 注入故障。"""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self.consumed: list[bytes] = []

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        item = self._lines.pop(0)
        if isinstance(item, Exception):
            raise item
        self.consumed.append(item)
        return item


class FakeResponse:
    def __init__(self, content: FakeContent, status: int = 200, error_body=None):
        self.status = status
        self.content = content
        self._error_body = error_body

    async def json(self, content_type=None):
        return self._error_body if self._error_body is not None else {}


class FakeSession:
    """aiohttp.ClientSession 替身：记录 post 入参，返回受控响应。"""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.posts: list[dict] = []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakePostContext(self.response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePostContext:
    def __init__(self, response: FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class OpenAIStreamTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        turn_trace.reset_turn_trace()
        self._saved = {
            name: getattr(OpenAIManager, name)
            for name in (
                "_stream_enabled",
                "_api_style",
                "_sessions",
                "_base_url",
                "_session_key",
                "_extra_body",
                "_tool_calls_enabled",
                "_response_timeout",
                "_initialized",
                "_enabled",
                "last_error",
            )
        }
        # 基类 send 链在 _initialized/_enabled 不满足时会触发真实配置装载
        # 并把后端判为禁用——测试环境直接置位，避免拉起 ConfigManager。
        OpenAIManager._initialized = True
        OpenAIManager._enabled = True
        OpenAIManager.last_error = None
        OpenAIManager._sessions = {}
        OpenAIManager._session_key = "test:stream"
        OpenAIManager._extra_body = {}
        self._session_key = "test:stream"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(OpenAIManager, name, value)
        turn_trace.reset_turn_trace()

    def _patch_session(self, response: FakeResponse) -> FakeSession:
        session = FakeSession(response)
        patcher = mock.patch(
            "core.openai.aiohttp.ClientSession",
            side_effect=lambda **kwargs: session,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return session


class ParseSSELineTest(unittest.TestCase):
    def test_data_json_returns_dict(self):
        self.assertEqual({"a": 1}, parse_sse_line('data: {"a": 1}\n'))

    def test_done_marker_returns_sentinel(self):
        self.assertIs(_SSE_DONE, parse_sse_line("data: [DONE]"))

    def test_non_data_lines_return_none(self):
        for line in ("", "\n", ": keep-alive", "event: message", "retry: 100"):
            self.assertIsNone(parse_sse_line(line), line)

    def test_bad_json_returns_none(self):
        self.assertIsNone(parse_sse_line("data: not-json"))
        self.assertIsNone(parse_sse_line('data: [1, 2]'))  # 非 dict


class StreamingHappyPathTest(OpenAIStreamTestBase):
    async def test_delta_assembly_yields_segments(self):
        """逐字 delta + 句读 → 句段序列（首段在第二句到达前已产出）。"""
        lines = [
            sse(role="assistant"),
            sse("今天"),
            sse("天气"),
            sse("很好。"),
            sse("我们出去玩吧。"),
            SSE_DONE_LINE,
        ]
        session = self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                segments = [
                    seg
                    async for seg in OpenAIManager.stream_response_segments("你好")
                ]

        self.assertEqual(["今天天气很好。", "我们出去玩吧。"], segments)
        payload = session.posts[0]["json"]
        self.assertIs(True, payload["stream"])

    async def test_extra_body_stream_is_overridden(self):
        """extra_body 显式 stream=False：开关开后仍以开关为准（stream=True）。"""
        OpenAIManager._extra_body = {"stream": False}
        lines = [sse("好的好的。"), SSE_DONE_LINE]
        session = self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                segments = [
                    seg async for seg in OpenAIManager.stream_response_segments("你好")
                ]
        self.assertIs(True, session.posts[0]["json"]["stream"])
        self.assertEqual(["好的好的。"], segments)

    async def test_history_records_full_text(self):
        lines = [sse("今天天气很好。"), sse("我们出去玩吧。"), SSE_DONE_LINE]
        self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                _ = [seg async for seg in OpenAIManager.stream_response_segments("你好")]
        history = OpenAIManager._sessions[self._session_key]
        self.assertEqual(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "今天天气很好。我们出去玩吧。"},
            ],
            history,
        )

    async def test_trace_records_llm_span_and_chars(self):
        lines = [sse("今天天气很好。"), SSE_DONE_LINE]
        self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch.dict(os.environ, {turn_trace.ENV_TURN_TRACE_ENABLED: "1"}):
            with mock.patch("core.openai.get_all_tools", return_value=[]):
                with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                    trace = turn_trace.begin_turn("xiaoai_asr", asr_chars=2, turn_id=1)
                    _ = [
                        seg async for seg in OpenAIManager.stream_response_segments("你好")
                    ]
        summary = trace.summary()
        self.assertEqual(7, summary["llm_chars"])
        self.assertNotEqual(-1.0, summary["llm_ms"])
        # 首个非空文本 delta（voice-latency T06）：流式路径必有；且不晚于流结束。
        self.assertNotEqual(-1.0, summary["llm_first_token_ms"])
        self.assertLessEqual(summary["llm_first_token_ms"], summary["llm_ms"])


class StreamingRobustnessTest(OpenAIStreamTestBase):
    async def test_empty_and_non_content_deltas_are_skipped(self):
        lines = [
            sse(role="assistant"),
            sse(""),
            sse(None, reasoning_content="thinking..."),
            sse(None, tool_calls=[{"id": "x"}]),
            sse("好的好的。"),
            SSE_DONE_LINE,
            sse("迟到的事件"),  # [DONE] 之后的多余行：跳过不崩
        ]
        self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                segments = [
                    seg async for seg in OpenAIManager.stream_response_segments("你好")
                ]
        self.assertEqual(["好的好的。"], segments)

    async def test_tail_without_punctuation_is_flushed(self):
        lines = [sse("好的好的。"), sse("尾巴没有标点"), SSE_DONE_LINE]
        self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                segments = [
                    seg async for seg in OpenAIManager.stream_response_segments("你好")
                ]
        self.assertEqual(["好的好的。", "尾巴没有标点"], segments)

    async def test_interrupted_stream_degrades_without_killing_session(self):
        """连接中断（standards-review 修复项）：吞错降级、已产出段保留、
        不入历史、last_error 可观测——上游异常不杀会话（与非流式同口径）。"""
        lines = [
            sse("今天天气很好。"),
            RuntimeError("connection reset"),
        ]
        self._patch_session(FakeResponse(FakeContent(lines)))
        collected: list[str] = []

        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                async for seg in OpenAIManager.stream_response_segments("你好"):
                    collected.append(seg)

        self.assertEqual(["今天天气很好。"], collected)  # 已产出段有效
        # 半截回答不入会话历史（与非流式口径一致；setdefault 空槽无害）
        self.assertEqual([], OpenAIManager._sessions.get(self._session_key, ["x"]))
        self.assertIn("RuntimeError", OpenAIManager.last_error)

    async def test_http_error_degrades_to_no_segments(self):
        response = FakeResponse(FakeContent([]), status=400, error_body={"error": "bad"})
        self._patch_session(response)
        with mock.patch("core.openai.get_all_tools", return_value=[]):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                segments = [
                    seg async for seg in OpenAIManager.stream_response_segments("你好")
                ]
        # 零段 ⇒ 上层对话循环走「没有收到回复」降级播报并继续听
        self.assertEqual([], segments)
        self.assertIn("HTTP 400", OpenAIManager.last_error)


class StreamingDegradationTest(OpenAIStreamTestBase):
    """降级面：开关关 / 非 chat_completions → 基类单句序列；工具轮探测降级。"""

    async def test_stream_disabled_by_default_uses_base_path(self):
        self.assertFalse(OpenAIManager.stream_enabled())
        with mock.patch.object(
            OpenAIManager, "send", mock.AsyncMock(return_value="整段回复")
        ) as send:
            segments = [
                seg async for seg in OpenAIManager.stream_response_segments("你好")
            ]
        self.assertEqual(["整段回复"], segments)
        send.assert_awaited_once_with("你好", wait_response=True)

    async def test_non_chat_completions_style_degrades(self):
        with mock.patch.object(OpenAIManager, "_api_style", "openai_responses"):
            self.assertFalse(OpenAIManager.stream_enabled())
        with mock.patch.object(OpenAIManager, "_api_style", "anthropic_messages"):
            self.assertFalse(OpenAIManager.stream_enabled())

    async def test_tool_loop_round_falls_back_to_non_streaming(self):
        """回环轮：探测请求见 tool_calls 增量（未产出段）→ 整轮转非流式。"""
        probe_lines = [
            sse(None, tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": ""}}]),
        ]
        session = self._patch_session(FakeResponse(FakeContent(probe_lines)))
        with mock.patch(
            "core.openai.get_all_tools", return_value=[{"type": "function"}]
        ):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                with mock.patch.object(
                    OpenAIManager, "send", mock.AsyncMock(return_value="工具轮回复")
                ) as send:
                    segments = [
                        seg async for seg in OpenAIManager.stream_response_segments("你好")
                    ]
        self.assertEqual(["工具轮回复"], segments)
        send.assert_awaited_once()
        self.assertEqual(1, len(session.posts))  # 仅一次被放弃的流式探测

    async def test_tool_round_final_answer_streams(self):
        """终答轮流式（ticket AC3）：tools 注册但模型不调工具 → 流式到底。"""
        lines = [
            sse(role="assistant"),
            sse("今天天气很好。"),
            SSE_DONE_LINE,
        ]
        session = self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch(
            "core.openai.get_all_tools", return_value=[{"type": "function"}]
        ):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                with mock.patch.object(
                    OpenAIManager, "send", mock.AsyncMock(return_value="不该走到这")
                ) as send:
                    segments = [
                        seg async for seg in OpenAIManager.stream_response_segments("你好")
                    ]
        self.assertEqual(["今天天气很好。"], segments)
        send.assert_not_awaited()  # 未降级
        payload = session.posts[0]["json"]
        self.assertEqual([{"type": "function"}], payload["tools"])  # tools 照常注入

    async def test_tool_calls_after_first_segment_are_dropped(self):
        """已产出句段后到达的 tool_calls 增量：丢弃（该轮按纯文本终答）。"""
        lines = [
            sse("好的好的。"),
            sse(None, tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": ""}}]),
            sse("补充一句。"),
            SSE_DONE_LINE,
        ]
        self._patch_session(FakeResponse(FakeContent(lines)))
        with mock.patch(
            "core.openai.get_all_tools", return_value=[{"type": "function"}]
        ):
            with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                segments = [
                    seg async for seg in OpenAIManager.stream_response_segments("你好")
                ]
        self.assertEqual(["好的好的。", "补充一句。"], segments)


class BaseClassSingleSegmentTest(unittest.IsolatedAsyncioTestCase):
    """基类默认：非流式后端退化为单句序列（对话控制器同一消费路径）。"""

    class FakeManager(BackendManagerBase):
        CONFIG_PREFIX = "fake"
        LOG_MODULE = "Fake"
        reply = "整段回复"
        _enabled = True
        _initialized = True

        @classmethod
        async def _request_completion(cls, text, stats=None):
            return cls.reply

    async def test_single_segment_for_non_streaming_backend(self):
        segments = [
            seg async for seg in self.FakeManager.stream_response_segments("你好")
        ]
        self.assertEqual(["整段回复"], segments)

    async def test_empty_reply_yields_nothing(self):
        self.FakeManager.reply = None
        try:
            segments = [
                seg async for seg in self.FakeManager.stream_response_segments("你好")
            ]
        finally:
            self.FakeManager.reply = "整段回复"
        self.assertEqual([], segments)

    async def test_stream_enabled_default_false(self):
        self.assertFalse(BackendManagerBase.stream_enabled())
        self.assertFalse(self.FakeManager.stream_enabled())


class ToolLoopGuardTest(OpenAIStreamTestBase):
    """工具回环与 reasoning 回传守卫（ticket 03 验收：与改前一致）。

    守卫语义（openai.py T8.2）：thinking 模式带 tools 时，回填的 assistant
    消息必须完整回传响应携带的 reasoning_content（缺失直接 400）；仅当
    响应确实携带时才回传。回环轮走非流式 _request_completion——本组用例
    同时实证「注入 tools 的轮次不经 SSE」。
    """

    def _tool_call_body(self, *, with_reasoning: bool) -> dict:
        message: dict = {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        }
        if with_reasoning:
            message["reasoning_content"] = "先查天气再回答"
        return {
            "choices": [
                {"finish_reason": "tool_calls", "message": message},
            ]
        }

    _FINAL_BODY = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "今天晴，适合出门。"}}
        ]
    }

    async def _run_tool_round(self, with_reasoning: bool) -> list[dict]:
        bodies = [self._tool_call_body(with_reasoning=with_reasoning), self._FINAL_BODY]
        seen_payloads: list[dict] = []

        async def fake_post(url, payload, headers):
            seen_payloads.append(payload)
            return bodies[len(seen_payloads) - 1]

        # 探测期：流式请求首事件即 tool_calls 增量 → 整轮转非流式回环
        probe_lines = [
            sse(None, tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": ""}}]),
        ]
        self._patch_session(FakeResponse(FakeContent(probe_lines)))
        with mock.patch("core.openai.get_all_tools", return_value=[{"type": "function"}]):
            with mock.patch.object(OpenAIManager, "_post_llm", side_effect=fake_post):
                with mock.patch("core.openai.execute_tool", mock.AsyncMock(return_value="晴")):
                    with mock.patch.object(OpenAIManager, "_stream_enabled", True):
                        segments = [
                            seg
                            async for seg in OpenAIManager.stream_response_segments("天气如何")
                        ]
        self.assertEqual(["今天晴，适合出门。"], segments)
        return seen_payloads

    async def test_reasoning_content_is_passed_back_in_tool_loop(self):
        payloads = await self._run_tool_round(with_reasoning=True)
        self.assertEqual(2, len(payloads))
        assistant_msgs = [
            m for m in payloads[1]["messages"] if m.get("role") == "assistant"
        ]
        self.assertEqual(1, len(assistant_msgs))
        self.assertEqual("先查天气再回答", assistant_msgs[0].get("reasoning_content"))
        self.assertIn("tool_calls", assistant_msgs[0])
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(1, len(tool_msgs))
        self.assertEqual("call-1", tool_msgs[0]["tool_call_id"])

    async def test_absent_reasoning_content_is_not_synthesized(self):
        payloads = await self._run_tool_round(with_reasoning=False)
        assistant_msgs = [
            m for m in payloads[1]["messages"] if m.get("role") == "assistant"
        ]
        self.assertNotIn("reasoning_content", assistant_msgs[0])

    async def test_tool_rounds_fall_back_to_non_streaming_transport(self):
        """工具回环轮的传输面实证：非流式回环请求 payload stream=False。"""
        payloads = await self._run_tool_round(with_reasoning=True)
        for payload in payloads:
            self.assertIs(False, payload["stream"])


if __name__ == "__main__":
    unittest.main()
