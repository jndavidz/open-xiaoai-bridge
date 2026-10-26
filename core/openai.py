"""OpenAI-compatible service manager.

This module talks to any OpenAI-compatible Chat Completions endpoint,
including Hermes Agent API server mode.

接口规格（api_style）支持三种上游协议表面：
    - chat_completions   POST {base}/chat/completions（默认，原版唯一支持的）
    - openai_responses   POST {base}/responses（OpenAI Responses API；Aurora 默认表面）
    - anthropic_messages POST {base}/messages（Anthropic Messages API）

内部会话历史统一存 chat 格式（{role, content}），仅在出口按 api_style 转换，
切换接口规格不影响既有会话记忆。

公共骨架（记账 / 超时 / TTS 委托 / 配置装载 / 埋点）在
core.backend_base.BackendManagerBase；本模块只保留传输核。
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from core.backend_base import BackendManagerBase
from core.services import conversation_memory
from core.tools.registry import get_all_tools, execute_tool, is_external_network_tool
from core.utils import turn_trace
from core.utils.logger import logger
from core.utils.sentence_split import SentenceSegmenter


# 支持的接口规格（与 admin_api.CONFIG_SCHEMA 的 options 保持一致）
API_STYLES = ("chat_completions", "openai_responses", "anthropic_messages")

# SSE 流结束哨兵（data: [DONE]）
_SSE_DONE = object()


class _ToolLoopDetected(Exception):
    """流式探测轮检测到 tool_calls 增量：放弃流式，整轮转非流式回环。"""


def parse_sse_line(line: str) -> Any:
    """解析一行 SSE 文本：data JSON → dict；data [DONE] → _SSE_DONE；其余 → None。

    空行、注释（: keep-alive）、非 data 前缀、坏 JSON 一律返回 None
    （跳过，不中断流）——与上游 SSE 兼容实现的容错口径一致。
    """
    text = line.strip()
    if not text.startswith("data:"):
        return None
    payload = text[len("data:") :].strip()
    if payload == "[DONE]":
        return _SSE_DONE
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _delta_has_tool_calls(event: dict) -> bool:
    """SSE 事件是否携带 tool_calls 增量（探测降级的触发信号）。"""
    choices = event.get("choices")
    if not choices or not isinstance(choices[0], dict):
        return False
    delta = choices[0].get("delta")
    return isinstance(delta, dict) and bool(delta.get("tool_calls"))


def _usage_tokens(usage: Any) -> dict[str, Any]:
    """上游 usage 字典 → note_usage_tokens 关键字参数（两种命名都认；
    非 dict / 缺失返回空 dict = 未观测，台账按字符估算）。"""
    if not isinstance(usage, dict):
        return {}
    return {
        "prompt": usage.get("prompt_tokens", usage.get("input_tokens")),
        "completion": usage.get("completion_tokens", usage.get("output_tokens")),
    }


class OpenAIManager(BackendManagerBase):
    """Manager for OpenAI-compatible chat backends."""

    CONFIG_PREFIX = "openai"
    ENABLE_ENV_VARS = ("OPENAI_ENABLE",)
    LOG_MODULE = "OpenAI"

    # 上游接口规格：chat_completions | openai_responses | anthropic_messages
    _api_style = "chat_completions"
    _base_url = "http://127.0.0.1:8000/v1"
    _api_key = ""
    _model = "gpt-4o-mini"
    _session_key = "agent:default:open-xiaoai-bridge"
    # Optional header used to send session_key to the server (e.g. Hermes'
    # "X-Hermes-Session-Key" for long-term memory scoping). Standard OpenAI /
    # Ollama / LM Studio ignore the unknown header; set empty to disable.
    _session_header = "X-Hermes-Session-Key"
    # 换话题暗号的一次性信令标志（spec 票 01；force_new_session() 置位，
    # _headers() 消费即清）
    _force_new_session = False
    # 对话记忆（spec 票 04）：JSONL 落 data/memory/，注入触发状态
    _memory_config: dict[str, Any] = {}
    _memory_dir = Path(__file__).resolve().parents[1] / "data" / "memory"
    _last_response_at: float | None = None
    # 本轮待注入摘要（_prepare_memory_summary 异步预计算，_build_messages 同步读）
    _pending_memory_summary: str = ""
    DEFAULT_SESSION_KEY = "agent:default:open-xiaoai-bridge"
    _system_prompt = ""
    # Per-session persona override: session_key -> system prompt.
    # Takes precedence over _system_prompt when present (e.g. tutor vs Jarvis).
    _session_system_prompts: dict[str, str] = {}
    _temperature: float | None = None
    _max_tokens: int | None = None
    _history_max_messages = 20
    _extra_body: dict[str, Any] = {}
    # 工具层（Agent 1.5 / 阶段 1.5）：function calling 回环开关与轮数上限
    _tool_calls_enabled = True
    _max_tool_rounds = 3
    # 流式开关（P1 ticket 03）：per-provider 配置 openai.stream，默认关
    _stream_enabled = False
    _sessions: dict[str, list[dict[str, str]]] = {}

    @classmethod
    def _apply_config(cls, config: dict):
        """装载 OpenAI 传输核专属配置。"""
        cls._base_url = str(config.get("base_url", "http://127.0.0.1:8000/v1")).rstrip("/")
        cls._api_key = str(config.get("api_key", "") or "")
        cls._model = str(config.get("model", "gpt-4o-mini"))
        api_style = str(config.get("api_style", "chat_completions") or "chat_completions").strip()
        if api_style not in API_STYLES:
            logger.warning(
                f"[OpenAI] Unknown api_style {api_style!r}, falling back to chat_completions"
            )
            api_style = "chat_completions"
        cls._api_style = api_style
        cls._session_key = str(config.get("session_key", cls.DEFAULT_SESSION_KEY))
        cls._session_header = str(config.get("session_header", "X-Hermes-Session-Key") or "").strip()
        cls._system_prompt = str(config.get("system_prompt", "") or "")
        # Optional static overrides from config: {"agent:tutor:home": "..."}
        sp = config.get("session_system_prompts")
        if isinstance(sp, dict):
            cls._session_system_prompts = {
                str(k): str(v) for k, v in sp.items() if v
            }
        cls._temperature = cls._optional_float(config.get("temperature"))
        cls._max_tokens = cls._optional_int(config.get("max_tokens"))
        cls._extra_body = config.get("extra_body", {})
        if not isinstance(cls._extra_body, dict):
            cls._extra_body = {}
        # 工具层（Agent 1.5）：默认开启；max_tool_rounds 限制 tool_calls 回环深度
        cls._tool_calls_enabled = bool(config.get("tool_calls_enabled", True))
        cls._max_tool_rounds = max(1, int(config.get("max_tool_rounds", 3)))
        # 流式开关（P1 ticket 03）：默认关；热重载即生效（灰度回退 = 面板/配置一键关）
        cls._stream_enabled = bool(config.get("stream", False))
        # 对话记忆（spec 票 04）：memory 段整体读取；目录可经 MEMORY_DIR 覆盖
        cls._memory_config = dict(config.get("memory", {}) or {})
        memory_dir = os.environ.get("MEMORY_DIR", "").strip()
        cls._memory_dir = (
            Path(memory_dir) if memory_dir else Path(__file__).resolve().parents[1] / "data" / "memory"
        )

    @classmethod
    def _log_enabled(cls):
        logger.info(
            f"[OpenAI] Enabled, base_url={cls._base_url}, model={cls._model}, "
            f"api_style={cls._api_style}"
        )

    @classmethod
    def set_session_system_prompt(cls, prompt: str | None):
        """Override the system prompt for the CURRENT session at runtime.

        Pass a non-empty string to install a persona (e.g. tutor), or None/empty
        to fall back to the global ``system_prompt``.
        """
        key = cls._session_key
        if prompt:
            cls._session_system_prompts[key] = str(prompt)
            logger.info(f"[OpenAI] Session system prompt override installed for {key!r}")
        else:
            if cls._session_system_prompts.pop(key, None) is not None:
                logger.info(f"[OpenAI] Session system prompt override cleared for {key!r}")

    @classmethod
    def reset_session(cls, session_key: str | None = None):
        cls._sessions.pop(session_key or cls._session_key, None)

    @classmethod
    def force_new_session(cls):
        """换话题暗号置位（spec 票 01）：下一次请求带 X-Session-Action: new。

        one-shot 语义：标志在 _headers() 构造请求头时消费并立即清位——
        「下轮请求带信令」不因多次构建而重复发送。
        """
        cls._force_new_session = True

    @classmethod
    def _resolve_tts_provider(cls, tts_speaker: str | None) -> str:
        """Resolve an explicit provider while preserving the legacy speaker rule."""
        from core.services.tts.router import TTSRouter

        return TTSRouter.resolve_provider(cls._tts_provider, tts_speaker)

    @classmethod
    async def _post_llm(cls, url: str, payload: dict, headers: dict) -> dict:
        """发送一次 LLM 请求并取回 JSON body（HTTP>=400 抛 RuntimeError）。"""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=cls._response_timeout)
        ) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    message = body.get("error", body) if isinstance(body, dict) else body
                    raise RuntimeError(f"HTTP {response.status}: {message}")
                return body

    @classmethod
    def _tools_active(cls) -> bool:
        """工具回环三开关（AGENTS.md 排障判据）：总开关 × chat_completions
        风格 × 注册表非空——缺一即回退纯对话。流式降级与传输核共用此判定。"""
        return (
            cls._tool_calls_enabled
            and cls._api_style == "chat_completions"
            and bool(get_all_tools())
        )

    @classmethod
    def usage_model_label(cls) -> str:
        """用量记账的模型标签：真实上游模型名。"""
        return cls._model

    @staticmethod
    def _prompt_chars(messages: list[dict[str, str]]) -> int:
        """本次请求发送的 prompt 字符总数（system+历史+本轮，估算口径）。"""
        total = 0
        for message in messages:
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    total += len(content)
        return total

    @classmethod
    async def _request_completion(
        cls, text: str, stats: dict[str, int] | None = None
    ) -> str | None:
        session_key = cls._session_key
        history = cls._sessions.setdefault(session_key, [])
        await cls._prepare_memory_summary()  # 记忆摘要在 executor 预计算（同步构建前）
        messages = cls._build_messages(history, text)

        # 工具层（Agent 1.5 / 阶段 1.5）：仅 chat_completions 风格注入 tools。
        # 其它 api_style（anthropic/responses）本期不接工具，纯对话回退。
        use_tools = cls._tools_active()
        tools = get_all_tools() if use_tools else None

        for _ in range(cls._max_tool_rounds + 1):
            # 内部历史统一 chat 格式，出口按接口规格转换
            if cls._api_style == "anthropic_messages":
                url, payload, headers = cls._build_anthropic_request(messages)
            elif cls._api_style == "openai_responses":
                url, payload, headers = cls._build_responses_request(messages)
            else:
                url, payload, headers = cls._build_chat_completions_request(messages)

            if tools is not None:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            # 用量记账（P2 ticket 03）：一次 HTTP 往返 = 一次请求 + 一份 prompt。
            turn_trace.note_llm_request()
            turn_trace.note_prompt_chars(cls._prompt_chars(messages))

            try:
                body = await cls._post_llm(url, payload, headers)
            except RuntimeError as exc:
                cls.last_error = f"{type(exc).__name__}: {exc}"
                logger.error(f"[OpenAI] Chat completion failed: {cls.last_error}")
                return None

            # 上游自报 usage 时记真实 token（缺失则台账按字符估算，口径见脚注）
            usage = body.get("usage") if isinstance(body, dict) else None
            turn_trace.note_usage_tokens(**_usage_tokens(usage))

            choices = body.get("choices") if isinstance(body, dict) else None
            choice = choices[0] if choices else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            finish = choice.get("finish_reason")

            # —— 工具调用回环 ——
            if finish == "tool_calls" and tools is not None:
                tool_calls = msg.get("tool_calls") or []
                if tool_calls:
                    # 回填 assistant 消息；reasoning_content 守卫见下。
                    assistant_msg = {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                    rc = msg.get("reasoning_content")
                    if rc:
                        # T8.2 守卫：thinking 模式带 tools 时，后续每轮必须完整回传
                        # assistant 的 reasoning_content，缺失直接 400。仅当响应确实
                        # 携带时才带回——非 thinking 模型不会带此字段，故对它们无影响。
                        assistant_msg["reasoning_content"] = rc
                    messages.append(assistant_msg)
                    for tc in tool_calls:
                        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                        result = await execute_tool(fn.get("name"), fn.get("arguments", "{}"))
                        if is_external_network_tool(fn.get("name")):
                            # 联网口径：出公网的工具调用（weather_get）；LAN 工具不计
                            turn_trace.note_web_calls()
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "content": result,
                        })
                    logger.info(
                        f"[OpenAI] tool round: {[tc.get('function', {}).get('name') for tc in tool_calls]}"
                    )
                    if stats is not None:
                        stats["tool_rounds"] = int(stats.get("tool_rounds", 0)) + 1
                    continue  # 带着工具结果再请求一轮
                logger.warning("[OpenAI] finish_reason=tool_calls but empty tool_calls, fall back to text")

            # —— 普通文本完成 ——
            if cls._api_style == "anthropic_messages":
                response_text = cls._extract_anthropic_text(body)
            elif cls._api_style == "openai_responses":
                response_text = cls._extract_responses_text(body)
            else:
                response_text = cls._extract_response_text(body)

            if response_text:
                cls._append_history(history, text, response_text)
            return response_text

        logger.warning("[OpenAI] tool rounds exceeded max_tool_rounds, no final text returned")
        return None

    # —— 流式响应通道（P1 ticket 03：仅 chat_completions + 开关开） ----------

    @classmethod
    def stream_enabled(cls) -> bool:
        """流式开关：仅 chat_completions 风格生效（其余两种风格明确降级非流式）。"""
        return cls._stream_enabled and cls._api_style == "chat_completions"

    @classmethod
    async def stream_response_segments(cls, text: str):
        """SSE 流式增量组装 → 句段序列（对话控制器统一消费路径）。

        降级面（全部走基类「单句序列」，行为与改前一致）：
          - 开关关（默认）/ 接口规格非 chat_completions；
          - 工具回环轮：带 tools 的请求以「探测式流式」发起——首个
            tool_calls 增量到达且尚未产出句段时，放弃流式、整轮转
            非流式（既有 _request_completion 回环路径原样复用，
            reasoning 回传守卫语义不变）；模型不调工具的轮次（终答轮）
            流式到底。探测降级的代价是回环轮多一次被放弃的请求。
        """
        if not cls.stream_enabled():
            async for segment in super().stream_response_segments(text):
                yield segment
            return

        tools = get_all_tools() if cls._tools_active() else None
        logger.user_speech(text, module=f"{cls.LOG_MODULE}({cls._session_key})")
        turn_trace.mark_llm_start()
        # 用量记账维度（P2 ticket 03）：流式路径不经过 _run_transport，在此注入。
        turn_trace.note_context(
            backend=cls.LOG_MODULE,
            model=cls.usage_model_label(),
            session_key=cls._session_key,
        )
        try:
            generator = cls._stream_chat_completions_segments(
                text,
                tools=tools,
                tool_probe=tools is not None,
            )
            try:
                async for segment in generator:
                    yield segment
                return
            except _ToolLoopDetected:
                pass  # 回环轮：未产出任何段，整轮转非流式
            async for segment in super().stream_response_segments(text):
                yield segment
        finally:
            turn_trace.mark_llm_end()

    @classmethod
    async def _stream_chat_completions_segments(
        cls,
        text: str,
        *,
        tools: list | None = None,
        tool_probe: bool = False,
    ):
        """chat_completions SSE 流式传输核：增量文本组装 + 句读切分。

        tool_probe：带 tools 的轮次探测降级——首个 tool_calls 增量到达且
        尚未产出句段时抛 _ToolLoopDetected（外层转非流式回环）；已产出
        句段后到达的 tool_calls 增量被丢弃（该轮按纯文本终答，记 WARNING）。
        """
        session_key = cls._session_key
        history = cls._sessions.setdefault(session_key, [])
        await cls._prepare_memory_summary()  # 记忆摘要在 executor 预计算（同步构建前）
        messages = cls._build_messages(history, text)
        url, payload, headers = cls._build_chat_completions_request(messages)
        payload["stream"] = True  # 覆盖 extra_body 里的显式 stream，以开关为准
        if tools is not None:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        segmenter = SentenceSegmenter()
        full_parts: list[str] = []
        first_delta_marked = False
        completed = False
        yielded = False
        # 用量记账（P2 ticket 03）：一次 SSE POST = 一次请求；探测降级被放弃
        # 的请求也如实计入（真实成本）。prompt 字符为发起时的完整 messages。
        turn_trace.note_llm_request()
        turn_trace.note_prompt_chars(cls._prompt_chars(messages))
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=cls._response_timeout)
            ) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status >= 400:
                        body = await response.json(content_type=None)
                        message = body.get("error", body) if isinstance(body, dict) else body
                        raise RuntimeError(f"HTTP {response.status}: {message}")
                    async for raw_line in response.content:
                        event = parse_sse_line(raw_line.decode("utf-8", "replace"))
                        if event is _SSE_DONE:
                            break  # 上游结束标记：迟到的后续行不再消费
                        if event is None:
                            continue  # 空行 / 注释 / 坏 JSON 行：跳过
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            # 请求了 stream_options.include_usage 的上游会在
                            # 末帧带 usage；未请求时缺失，台账按字符估算
                            turn_trace.note_usage_tokens(**_usage_tokens(usage))
                        if _delta_has_tool_calls(event):
                            if tool_probe and not yielded:
                                raise _ToolLoopDetected(text)
                            logger.warning(
                                "[OpenAI] tool_calls delta after streamed segments; "
                                "dropped (streamed turn stays text-only)"
                            )
                            continue
                        delta_text = cls._extract_delta_content(event)
                        if not delta_text:
                            continue  # 空 delta / role 帧 / reasoning 帧
                        full_parts.append(delta_text)
                        if not first_delta_marked:
                            # 首个非空文本 delta（voice-latency T06）：L1 的
                            # 桥接侧观测点，补 llm_ms 只记流结束的缺口；
                            # mark 首次胜出，本地 flag 只省后续轮的锁开销。
                            first_delta_marked = True
                            turn_trace.mark_llm_first_token()
                        for segment in segmenter.feed(delta_text):
                            yielded = True
                            yield segment
            tail = segmenter.flush()
            if tail:
                yielded = True
                yield tail
            completed = True  # 流自然结束（迭代器耗尽）= 正常完成
        except _ToolLoopDetected:
            raise
        except Exception as exc:
            # 与非流式 _request_completion 同口径：上游异常不杀会话——
            # 已产出句段保留、本轮按已有内容收尾（零段则上层走「没有收到
            # 回复」降级播报并继续听）。灰度回退的前提是流式故障可容忍。
            cls.last_error = f"{type(exc).__name__}: {exc}"
            turn_trace.note_llm_error()
            logger.error(f"[OpenAI] stream request failed: {cls.last_error}")
        finally:
            # 与非流式口径一致：只有完整回复入会话历史——异常中断的半截
            # 回答不入（turn_trace 照记，便于排障）。
            full_text = "".join(full_parts)
            if completed and full_text:
                cls._append_history(history, text, full_text)
                turn_trace.note_reply_chars(len(full_text))
                logger.ai_response(
                    full_text, module=f"{cls.LOG_MODULE}({cls._session_key})"
                )

    @staticmethod
    def _extract_delta_content(event: dict) -> str:
        """从 SSE 事件取增量文本（choices[0].delta.content）。

        只组 content：reasoning_content（thinking 模型）与 tool_calls 帧
        不进终答文本——流式轮是终答轮，无后续回环，无需回传。
        """
        choices = event.get("choices")
        if not choices or not isinstance(choices[0], dict):
            return ""
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            return ""
        content = delta.get("content")
        return content if isinstance(content, str) else ""

    @classmethod
    def _build_chat_completions_request(cls, messages: list[dict[str, str]]):
        payload: dict[str, Any] = {
            "model": cls._model,
            "messages": messages,
            "stream": False,
            **cls._extra_body,
        }
        if cls._temperature is not None:
            payload["temperature"] = cls._temperature
        if cls._max_tokens is not None:
            payload["max_tokens"] = cls._max_tokens
        return cls._chat_completions_url(), payload, cls._headers()

    @classmethod
    def _build_responses_request(cls, messages: list[dict[str, str]]):
        """OpenAI Responses API：system 人设走 instructions，历史走 input 数组。"""
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        payload: dict[str, Any] = {
            "model": cls._model,
            "input": [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m["role"] != "system"
            ],
            "stream": False,
            **(cls._extra_body or {}),
        }
        if system:
            payload["instructions"] = system
        if cls._max_tokens is not None:
            payload["max_output_tokens"] = cls._max_tokens
        if cls._temperature is not None:
            payload["temperature"] = cls._temperature
        url = (
            cls._base_url
            if cls._base_url.endswith("/responses")
            else cls._base_url.rstrip("/") + "/responses"
        )
        return url, payload, cls._headers("openai_responses")

    @classmethod
    def _build_anthropic_request(cls, messages: list[dict[str, str]]):
        """Anthropic Messages API：system 独立字段；max_tokens 为必填项。"""
        system = next((m["content"] for m in messages if m["role"] == "system"), None)
        payload: dict[str, Any] = {
            "model": cls._model,
            "messages": [
                {"role": m["role"], "content": m["content"]}
                for m in messages
                if m["role"] in ("user", "assistant")
            ],
            "max_tokens": cls._max_tokens or 1024,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if cls._temperature is not None:
            payload["temperature"] = cls._temperature
        url = (
            cls._base_url
            if cls._base_url.endswith("/messages")
            else cls._base_url.rstrip("/") + "/messages"
        )
        return url, payload, cls._headers("anthropic_messages")

    @classmethod
    def _headers(cls, style: str | None = None) -> dict[str, str]:
        style = style or cls._api_style
        headers = {"Content-Type": "application/json"}
        if cls._api_key:
            headers["Authorization"] = f"Bearer {cls._api_key}"
            if style == "anthropic_messages":
                # Anthropic 官方鉴权头；同时保留 Bearer 兼容各类代理网关
                headers["x-api-key"] = cls._api_key
                headers["anthropic-version"] = "2023-06-01"
        # Scope server-side long-term memory to this session (e.g. Hermes'
        # X-Hermes-Session-Key). chat/completions stays stateless — the
        # messages array is still the source of turn history — so this does
        # not duplicate context.
        # 换话题信令消费：无论 header 门控是否成立都清位（review 修正——
        # session_header 留空的 plain-OpenAI 配置若永不清位，_maybe_memory_summary
        # 会因标志恒真而每轮注入记忆）。构建请求头即视为发送。
        force_new = cls._force_new_session
        if cls._force_new_session:
            cls._force_new_session = False
        if style == "chat_completions" and cls._session_header and cls._session_key:
            headers[cls._session_header] = cls._session_key
            # aurora 会话复用信令（spec topic-switch-and-memory 票 01）：叠加
            # X-Session-Key（与 session_header 并存同值——Hermes 配置下两头并存，
            # aurora 配置下幂等回写）。session_header 留空 = 纯净请求（plain
            # OpenAI 契约不变）；非 aurora 后端忽略未知头，无副作用。
            headers.setdefault("X-Session-Key", cls._session_key)
            if force_new:
                headers["X-Session-Action"] = "new"
        return headers

    @classmethod
    def _chat_completions_url(cls) -> str:
        if cls._base_url.endswith("/chat/completions"):
            return cls._base_url
        return cls._base_url.rstrip("/") + "/chat/completions"

    @classmethod
    def _build_messages(cls, history: list[dict[str, str]], text: str) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        system = (
            cls._session_system_prompts.get(cls._session_key)
            or cls._system_prompt
        )
        if system:
            messages.append({"role": "system", "content": system})
        # 对话记忆注入（spec 票 04）：上下文断档（TTL 过期/容器重启）或刚换
        # 话题时，把最近 N 轮摘要作为第二条 system 注入——bridge 是上下文唯一
        # 权威，aurora 不猜。注入失败降级为无摘要，绝不阻断请求。
        memory_summary = cls._maybe_memory_summary()
        if memory_summary:
            messages.append({"role": "system", "content": memory_summary})
        messages.extend(history[-cls._history_max_messages :] if cls._history_max_messages else [])
        messages.append({"role": "user", "content": text})
        return messages

    @classmethod
    def _maybe_memory_summary(cls) -> str:
        """记忆注入判据（同步版，供 _build_messages 同步构建路径调用）。

        触发 = 距上一轮完成超过 memory.gap_minutes **或** _force_new_session
        置位中（换话题清了本地历史，记忆文件是唯一的上下文来源）。
        文件 IO 已经 async 前置计算（_prepare_memory_summary，结果缓存在
        _pending_memory_summary），此处只读缓存——绝不在此做同步磁盘 IO。
        """
        return cls._pending_memory_summary or ""

    @classmethod
    async def _prepare_memory_summary(cls) -> None:
        """异步前置：把本轮要注入的摘要算好（文件 IO 在 executor）。

        调用点在各传输核构建 payload 之前（_build_messages 之前）——构建路径
        全同步，磁盘 IO 必须提前卸载到 executor（AGENTS.md 异步纪律；
        usage_ledger 同款模式）。
        """
        cfg = cls._memory_config or {}
        if not cfg.get("enabled"):
            cls._pending_memory_summary = ""
            return
        try:
            loop = asyncio.get_running_loop()
            now = time.time()
            gap_seconds = float(cfg.get("gap_minutes", 30)) * 60
            context_broken = cls._last_response_at is None or (
                now - cls._last_response_at
            ) > gap_seconds
            if not context_broken and not cls._force_new_session:
                cls._pending_memory_summary = ""
                return
            turns = await loop.run_in_executor(
                None,
                conversation_memory.recent_turns,
                cls._memory_dir,
                cls._session_key,
                int(cfg.get("max_turns", 5)),
                datetime.now(),
            )
            cls._pending_memory_summary = conversation_memory.summary_block(turns)
        except Exception as exc:  # noqa: BLE001 - 记忆注入绝不阻断对话
            cls._pending_memory_summary = ""
            logger.warning(f"[OpenAI] memory inject skipped: {type(exc).__name__}: {exc}")

    @classmethod
    async def _on_response_complete(cls, text: str, response_text: str):
        """B-30 票 04：一轮完整回复落账成功后写对话记忆 + 惰性清理过期文件。

        文件 IO 卸载到 executor（AGENTS.md 异步纪律）；写入/清理失败只记日志。
        """
        await super()._on_response_complete(text, response_text)
        cfg = cls._memory_config or {}
        if not cfg.get("enabled") or not response_text:
            return
        try:
            loop = asyncio.get_running_loop()
            now = datetime.now()
            cls._last_response_at = time.time()

            def _write():
                conversation_memory.append_turn(
                    cls._memory_dir, cls._session_key, text, response_text, now
                )
                conversation_memory.purge_expired(
                    cls._memory_dir, int(cfg.get("retain_days", 30)), now
                )

            await loop.run_in_executor(None, _write)
        except Exception as exc:  # noqa: BLE001 - 记忆写入绝不阻断对话
            logger.warning(f"[OpenAI] memory write failed: {type(exc).__name__}: {exc}")

    @classmethod
    def _append_history(cls, history: list[dict[str, str]], text: str, response_text: str):
        if cls._history_max_messages <= 0:
            return
        history.extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": response_text},
            ]
        )
        if len(history) > cls._history_max_messages:
            del history[: len(history) - cls._history_max_messages]

    @staticmethod
    def _extract_responses_text(body: Any) -> str | None:
        """OpenAI Responses API 取文本：优先聚合字段 output_text，
        否则遍历 output[] → message → output_text 片段拼接。"""
        if not isinstance(body, dict):
            return None
        aggregated = body.get("output_text")
        if isinstance(aggregated, str) and aggregated.strip():
            return aggregated.strip()
        chunks: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
        return "".join(chunks).strip() or None

    @staticmethod
    def _extract_anthropic_text(body: Any) -> str | None:
        """Anthropic Messages API 取文本：content[] 中 type=text 片段拼接。"""
        if not isinstance(body, dict):
            return None
        chunks = [
            part.get("text", "")
            for part in body.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(chunks).strip() or None

    @classmethod
    def _extract_response_text(cls, body: Any) -> str | None:
        if not isinstance(body, dict):
            return None
        choices = body.get("choices")
        if not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                return "".join(parts).strip() or None
        text = first.get("text")
        return text.strip() if isinstance(text, str) else None
