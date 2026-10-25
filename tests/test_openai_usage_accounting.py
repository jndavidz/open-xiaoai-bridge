"""OpenAI 传输核用量记账（P2 ticket 03）契约测试。

mock `_post_llm`（不发真实 HTTP）+ stub 原生扩展，覆盖：

  1. 单轮：requests=1、prompt_chars=system+用户、真实 usage token 入账；
  2. 工具回环：每轮 HTTP 请求各计 1 次、prompt 逐轮累加、
     weather_get（出公网）计联网、hass（LAN）不计；
  3. 上游不带 usage：token 字段保持 -1（台账按字符估算，口径在面板脚注）。
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
sys.modules.setdefault(
    "aiohttp",
    types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
)

from core.utils import turn_trace  # noqa: E402


def _chat_body(content=None, tool_calls=None, usage=None):
    message: dict = {"role": "assistant", "content": content or ""}
    finish = "tool_calls" if tool_calls else "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
    body: dict = {"choices": [{"message": message, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return body


class OpenAIUsageAccountingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = importlib.import_module("core.openai").OpenAIManager
        self.manager._enabled = True
        self.manager._api_style = "chat_completions"
        self.manager._tool_calls_enabled = True
        self.manager._max_tool_rounds = 3
        self.manager._model = "deepseek-chat"
        self.manager._temperature = None
        self.manager._max_tokens = None
        self.manager._extra_body = {}
        self.manager._session_header = ""
        self.manager._session_key = "agent:default:test"
        self.manager._system_prompt = "你是家庭助手。"
        self.manager._sessions.clear()
        turn_trace.reset_turn_trace(reset_counter=True)
        os_env = self.enterContext(mock.patch.dict("os.environ", {}))
        os_env[turn_trace.ENV_TURN_TRACE_ENABLED] = "1"
        self.trace = turn_trace.begin_turn(source="llm", turn_id=9001)

    def tearDown(self):
        turn_trace.reset_turn_trace(reset_counter=True)

    async def _run(self, bodies, tools=False):
        """按序返回 mock 响应体，跑一次传输核。"""
        responses = list(bodies)
        calls: list[dict] = []

        async def fake_post(url, payload, headers):
            calls.append(payload)
            return responses.pop(0)

        with mock.patch.object(self.manager, "_post_llm", staticmethod(fake_post)):
            stats = {"tool_rounds": 0}
            text = await self.manager._request_completion(
                "今天天气怎样", stats=stats
            )
        return text, stats, calls

    async def test_single_round_counts_request_prompt_and_real_tokens(self):
        body = _chat_body(
            content="今天晴", usage={"prompt_tokens": 50, "completion_tokens": 12}
        )
        text, stats, calls = await self._run([body])

        self.assertEqual("今天晴", text)
        self.assertEqual(1, len(calls))
        summary = self.trace.summary()
        self.assertEqual(1, summary["llm_requests"])
        # system + 用户消息字符
        self.assertEqual(len("你是家庭助手。") + len("今天天气怎样"), summary["prompt_chars"])
        self.assertEqual(50, summary["prompt_tokens"])
        self.assertEqual(12, summary["completion_tokens"])
        self.assertEqual(0, summary["web_calls"])
        self.assertEqual(0, stats["tool_rounds"])

    async def test_tool_round_accumulates_requests_and_web_calls(self):
        weather_call = [{
            "id": "c1", "type": "function",
            "function": {"name": "weather_get", "arguments": "{}"},
        }]
        hass_call = [{
            "id": "c2", "type": "function",
            "function": {"name": "hass_get_states", "arguments": "{}"},
        }]
        bodies = [
            _chat_body(tool_calls=weather_call,
                       usage={"prompt_tokens": 50, "completion_tokens": 5}),
            _chat_body(tool_calls=hass_call,
                       usage={"prompt_tokens": 60, "completion_tokens": 5}),
            _chat_body(content="晴", usage={"prompt_tokens": 70, "completion_tokens": 10}),
        ]

        with mock.patch("core.openai.execute_tool", new=mock.AsyncMock(return_value="ok")):
            text, stats, calls = await self._run(bodies)

        self.assertEqual("晴", text)
        self.assertEqual(3, len(calls))
        summary = self.trace.summary()
        self.assertEqual(3, summary["llm_requests"])
        self.assertEqual(1, summary["web_calls"])  # 仅 weather_get 出公网
        self.assertEqual(2, stats["tool_rounds"])
        self.assertEqual(50 + 60 + 70, summary["prompt_tokens"])
        self.assertEqual(5 + 5 + 10, summary["completion_tokens"])
        # prompt 字符逐轮累加（第二轮重发历史 + 工具结果）
        self.assertGreater(summary["prompt_chars"], len("你是家庭助手。") + len("今天天气怎样"))

    async def test_missing_usage_keeps_tokens_unobserved(self):
        body = _chat_body(content="好")  # 无 usage
        await self._run([body])
        summary = self.trace.summary()
        self.assertEqual(-1, summary["prompt_tokens"])
        self.assertEqual(-1, summary["completion_tokens"])
        self.assertEqual(1, summary["llm_requests"])
        self.assertGreater(summary["prompt_chars"], 0)


if __name__ == "__main__":
    unittest.main()
