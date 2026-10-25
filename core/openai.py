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

from typing import Any

import aiohttp

from core.backend_base import BackendManagerBase
from core.tools.registry import get_all_tools, execute_tool
from core.utils.logger import logger


# 支持的接口规格（与 admin_api.CONFIG_SCHEMA 的 options 保持一致）
API_STYLES = ("chat_completions", "openai_responses", "anthropic_messages")


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
        cls._session_key = str(config.get("session_key", "agent:default:open-xiaoai-bridge"))
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
    async def _request_completion(
        cls, text: str, stats: dict[str, int] | None = None
    ) -> str | None:
        session_key = cls._session_key
        history = cls._sessions.setdefault(session_key, [])
        messages = cls._build_messages(history, text)

        # 工具层（Agent 1.5 / 阶段 1.5）：仅 chat_completions 风格注入 tools。
        # 其它 api_style（anthropic/responses）本期不接工具，纯对话回退。
        use_tools = (
            cls._tool_calls_enabled
            and cls._api_style == "chat_completions"
            and bool(get_all_tools())
        )
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

            try:
                body = await cls._post_llm(url, payload, headers)
            except RuntimeError as exc:
                cls.last_error = f"{type(exc).__name__}: {exc}"
                logger.error(f"[OpenAI] Chat completion failed: {cls.last_error}")
                return None

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
        if style == "chat_completions" and cls._session_header and cls._session_key:
            headers[cls._session_header] = cls._session_key
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
        messages.extend(history[-cls._history_max_messages :] if cls._history_max_messages else [])
        messages.append({"role": "user", "content": text})
        return messages

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
