"""QwenPaw service manager.

This module talks to a running QwenPaw application through its HTTP
console task API and exposes the same minimal interface used by the
external conversation controller.

公共骨架（记账 / 超时 / TTS 委托 / 配置装载 / 埋点）在
core.backend_base.BackendManagerBase；本模块只保留传输核
（任务提交 + 轮询取结果 + 会话键解析）。
"""

import asyncio
from typing import Any

import aiohttp

from core.backend_base import BackendManagerBase
from core.utils import turn_trace
from core.utils.config import ConfigManager  # noqa: F401  兼容面：既有测试 patch core.qwenpaw.ConfigManager
from core.utils.logger import logger


class QwenPawManager(BackendManagerBase):
    """Manager for QwenPaw HTTP chat backend."""

    CONFIG_PREFIX = "qwenpaw"
    ENABLE_ENV_VARS = ("QWENPAW_ENABLE",)
    LOG_MODULE = "QwenPaw"

    _base_url = "http://127.0.0.1:8088"
    _agent_id = "default"
    _user_id = "open-xiaoai-bridge"
    _session_key = "agent:default:open-xiaoai-bridge"
    DEFAULT_SESSION_KEY = "agent:default:open-xiaoai-bridge"
    _send_path = "/api/console/chat/task"
    _task_status_path = "/api/console/chat/task/{task_id}"
    _poll_interval = 0.5
    _auth_token = ""
    _auth_header = "Authorization"
    _auth_scheme = "Bearer"

    @classmethod
    def _apply_config(cls, config: dict):
        """装载 QwenPaw 传输核专属配置。"""
        cls._base_url = str(config.get("base_url", "http://127.0.0.1:8088")).rstrip("/")
        cls._agent_id = str(config.get("agent_id", "default") or "default")
        cls._user_id = str(config.get("user_id", "open-xiaoai-bridge") or "open-xiaoai-bridge")
        cls._session_key = str(config.get("session_key", cls.DEFAULT_SESSION_KEY) or cls.DEFAULT_SESSION_KEY)
        cls._send_path = str(config.get("send_path", "/api/console/chat/task") or "/api/console/chat/task")
        cls._task_status_path = str(
            config.get("task_status_path", "/api/console/chat/task/{task_id}")
            or "/api/console/chat/task/{task_id}"
        )
        cls._poll_interval = max(0.1, float(config.get("poll_interval", 0.5)))
        cls._auth_token = str(config.get("auth_token", "") or "")
        cls._auth_header = str(config.get("auth_header", "Authorization")).strip() or "Authorization"
        cls._auth_scheme = str(config.get("auth_scheme", "Bearer")).strip()

    @classmethod
    def _log_enabled(cls):
        logger.info(
            f"[QwenPaw] Enabled, base_url={cls._base_url}, agent_id={cls._agent_id}"
        )

    @classmethod
    def _parse_session_key(cls, session_key: str | None = None) -> tuple[str, str]:
        """Split a session_key into (agent_id, session_id).

        The session_key uses the unified agent:<agentId>:<sessionId> format.
        The first two segments are the marker and agentId; everything after
        the second colon is the session id (which may itself contain colons).
        Falls back to the configured agent_id and the raw key when the value
        does not follow the convention, for backward compatibility.
        """
        key = session_key or cls._session_key or ""
        parts = key.split(":", 2)
        if len(parts) == 3 and parts[0] == "agent" and parts[1]:
            return parts[1], parts[2]
        return cls._agent_id, key

    @classmethod
    async def _request_completion(
        cls, text: str, stats: dict[str, int] | None = None
    ) -> str | None:
        # 用量记账（P2 ticket 03）：一次 agent 提交 = 一次记账请求（轮询不计）。
        turn_trace.note_llm_request()
        timeout = aiohttp.ClientTimeout(total=cls._response_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                cls._url_for_path(cls._send_path),
                json=cls._build_payload(text),
                headers=cls._headers(),
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {body}")

            task_id = body.get("task_id") if isinstance(body, dict) else None
            if not task_id:
                return cls._extract_response_text(body)

            return await cls._poll_task_result(session, str(task_id))

    @classmethod
    def _build_payload(cls, text: str) -> dict[str, Any]:
        _agent_id, session_id = cls._parse_session_key()
        return {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                }
            ],
            "session_id": session_id,
            "user_id": cls._user_id,
            "stream": True,
            "timeout": cls._response_timeout,
        }

    @classmethod
    def _headers(cls) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        agent_id, _session_id = cls._parse_session_key()
        if agent_id:
            headers["X-Agent-Id"] = agent_id
        if cls._auth_token:
            if cls._auth_scheme:
                headers[cls._auth_header] = f"{cls._auth_scheme} {cls._auth_token}"
            else:
                headers[cls._auth_header] = cls._auth_token
        return headers

    @classmethod
    async def _poll_task_result(
        cls,
        session: aiohttp.ClientSession,
        task_id: str,
    ) -> str | None:
        deadline = asyncio.get_running_loop().time() + cls._response_timeout
        while True:
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError(f"QwenPaw task timeout: {task_id}")

            async with session.get(
                cls._url_for_path(cls._task_status_path.format(task_id=task_id)),
                headers=cls._headers(),
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}: {body}")

            status = body.get("status") if isinstance(body, dict) else None
            if status == "finished":
                result = body.get("result") if isinstance(body, dict) else body
                return cls._extract_response_text(result)
            if status in {"failed", "cancelled"}:
                raise RuntimeError(f"QwenPaw task {status}: {body}")
            await asyncio.sleep(cls._poll_interval)

    @classmethod
    def _url_for_path(cls, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return cls._base_url + (path if path.startswith("/") else f"/{path}")

    @classmethod
    def _extract_response_text(cls, body: Any) -> str | None:
        texts: list[str] = []
        cls._collect_response_texts(body, texts)
        return "".join(texts).strip() or None

    @classmethod
    def _collect_response_texts(cls, value: Any, texts: list[str]):
        if isinstance(value, str):
            return
        if isinstance(value, list):
            for item in value:
                cls._collect_response_texts(item, texts)
            return
        if not isinstance(value, dict):
            return

        value_type = value.get("type")
        if value_type in {"turn_usage", "reasoning", "progress"}:
            return

        role = value.get("role")
        if role and role not in {"assistant", "system"}:
            return

        content = value.get("content")
        if content is not None:
            cls._collect_response_texts(content, texts)

        text = value.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)

        output = value.get("output")
        if output is not None:
            cls._collect_response_texts(output, texts)

        result = value.get("result")
        if result is not None:
            cls._collect_response_texts(result, texts)
