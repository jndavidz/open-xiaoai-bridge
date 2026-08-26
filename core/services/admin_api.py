"""后台管理面板 API（/admin 页面 + /api/admin/* REST）。

能力：
    - GET  /admin                    内嵌单页面板（无外部依赖）
    - GET  /api/admin/overview       各系统功能状态总览
    - GET  /api/admin/config         可编辑配置段（schema 驱动，密钥掩码）
    - PUT  /api/admin/config         写入运行时覆盖层并热生效
    - POST /api/admin/config/test    上游连通性预检（改前验证地址/key）
    - GET  /api/admin/logs           内存日志增量拉取（?after=<seq>）
    - POST /api/admin/logs/level     运行时调整日志级别

安全：
    - 所有 /api/admin/* 要求 Bearer/查询参数携带 ADMIN_TOKEN（hmac 常时比较）
    - ADMIN_TOKEN 未配置时一律 503 拒绝，避免局域网裸奔
    - 配置写入走白名单 schema + 运行时覆盖层，绝不触碰 config.py 源文件
"""

import asyncio
import hmac
import json
import logging
import os
import time
from typing import Any, Optional

import aiohttp
from aiohttp import web

from core.openclaw import OpenClawManager
from core.openai import OpenAIManager
from core.qwenpaw import QwenPawManager
from core.ref import get_app, get_kws, get_vad, get_xiaoai
from core.utils.config import ConfigManager
from core.utils.log_buffer import get_memory_log_handler
from core.utils.logger import logger
from core.utils.runtime_overrides import runtime_overrides

# ---------------------------------------------------------------- schema ----
# 可通过面板编辑的白名单字段。type: string | int | float | bool | secret | select
# secret 字段读取时只回掩码，保存留空=不修改，null=清除覆盖回落底层值。
CONFIG_SCHEMA: list[dict[str, Any]] = [
    {
        "id": "openai",
        "title": "AI 对话后端",
        "description": "贾维斯/老师等会话的上游服务。修改接口地址（含端口）、接口规格、模型名称或 API Key 后保存即热生效，下一次对话使用新配置。",
        "fields": [
            {
                "path": "openai.base_url",
                "label": "接口地址 Base URL",
                "type": "string",
                "placeholder": "https://api.deepseek.com/v1",
                "help": "含协议、主机、端口与路径前缀；按接口规格自动补全端点路径",
            },
            {
                "path": "openai.api_style",
                "label": "接口规格",
                "type": "select",
                "options": [
                    {"value": "chat_completions", "label": "OpenAI Chat Completions（默认）"},
                    {"value": "openai_responses", "label": "OpenAI Responses"},
                    {"value": "anthropic_messages", "label": "Anthropic Messages"},
                ],
                "help": "上游 API 协议表面；Aurora 网关两种 OpenAI 表面均支持，其默认为 Responses",
            },
            {
                "path": "openai.model",
                "label": "模型名称 Model",
                "type": "string",
                "placeholder": "deepseek-chat",
            },
            {
                "path": "openai.api_key",
                "label": "API Key",
                "type": "secret",
                "help": "留空表示不修改；「清除」回落到环境变量注入的原始 Key",
            },
            {
                "path": "openai.response_timeout",
                "label": "响应超时（秒）",
                "type": "int",
            },
        ],
    },
    {
        "id": "openclaw",
        "title": "OpenClaw 网关",
        "description": "未启用时可忽略；启用状态下修改 URL/Token 保存后需重连生效。",
        "fields": [
            {"path": "openclaw.url", "label": "WebSocket 地址", "type": "string"},
            {"path": "openclaw.token", "label": "认证 Token", "type": "secret"},
        ],
    },
    {
        "id": "qwenpaw",
        "title": "QwenPaw 工作台",
        "description": "未启用时可忽略。",
        "fields": [
            {"path": "qwenpaw.base_url", "label": "接口地址 Base URL", "type": "string"},
            {"path": "qwenpaw.auth_token", "label": "认证 Token", "type": "secret"},
        ],
    },
    {
        "id": "tts_doubao",
        "title": "豆包 TTS 凭据",
        "description": "tts_speaker 配置为豆包音色时使用。",
        "fields": [
            {"path": "tts.doubao.app_id", "label": "App ID", "type": "string"},
            {"path": "tts.doubao.access_key", "label": "Access Key", "type": "secret"},
        ],
    },
]

# path -> field 元数据（校验用）
_FIELD_INDEX: dict[str, dict[str, Any]] = {
    f["path"]: f for section in CONFIG_SCHEMA for f in section["fields"]
}

_SECRET_MASK_TAIL = 4


# ---------------------------------------------------------------- helpers ----
def _mask_secret(value: Any) -> dict[str, Any]:
    """密钥掩码视图：只暴露长度与尾 4 位。"""
    text = str(value or "")
    if not text:
        return {"set": False, "masked": ""}
    if len(text) <= _SECRET_MASK_TAIL:
        masked = "***"
    else:
        masked = "*" * min(12, len(text) - _SECRET_MASK_TAIL) + text[-_SECRET_MASK_TAIL:]
    return {"set": True, "masked": masked}


def _coerce_field_value(field: dict[str, Any], value: Any) -> Any:
    """按字段类型做严格转换，失败抛 ValueError（由 handler 转 400）。"""
    ftype = field.get("type", "string")
    if value is None:
        return None  # 清除覆盖
    if ftype == "select":
        allowed = {opt.get("value") for opt in field.get("options", [])}
        text = str(value).strip()
        if text not in allowed:
            raise ValueError(f"must be one of {sorted(a for a in allowed if a)}")
        return text
    if ftype == "int":
        return int(str(value).strip())
    if ftype == "float":
        return float(str(value).strip())
    if ftype == "bool":
        lowered = str(value).strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"invalid boolean: {value!r}")
    return str(value)


def _sanitize_patch(patch: Any) -> dict[str, Any]:
    """把前端 patch 过滤成白名单内的嵌套 dict，非法字段直接丢弃。"""
    if not isinstance(patch, dict):
        raise ValueError("patch must be an object")

    flat: dict[str, Any] = {}

    def _walk(node: dict[str, Any], prefix: str = "") -> None:
        for key, value in node.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict):
                _walk(value, prefix=f"{dotted}.")
            elif dotted in _FIELD_INDEX:
                flat[dotted] = value
            # 白名单外的键静默丢弃（防误写坏配置）

    _walk(patch)

    nested: dict[str, Any] = {}
    for dotted, raw in flat.items():
        field = _FIELD_INDEX[dotted]
        try:
            value = _coerce_field_value(field, raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{dotted}: {exc}") from exc
        parts = dotted.split(".")
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"conflicting override at {dotted}")
        node[parts[-1]] = value
    return nested


def _current_effective_values(dotted_paths: list[str]) -> dict[str, Any]:
    config = ConfigManager.instance().get_app_config() or {}
    values: dict[str, Any] = {}
    for dotted in dotted_paths:
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        values[dotted] = node
    return values


# --------------------------------------------------------------- handlers ----
class AdminAPI:
    """Admin 路由注册中心与处理器集合。"""

    def __init__(self, static_dir: Optional[str] = None):
        self.config = ConfigManager.instance()
        if static_dir is None:
            static_dir = os.path.join(
                os.path.dirname(__file__), "admin_static"
            )
        self.static_dir = static_dir
        self.started_at = time.time()

    # ---- auth ----

    @staticmethod
    def _expected_token() -> str:
        return os.environ.get("ADMIN_TOKEN", "").strip()

    @staticmethod
    def _provided_token(request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.query.get("token", "").strip()

    def _authorized(self, request: web.Request) -> bool:
        expected = self._expected_token()
        if not expected:
            return False
        provided = self._provided_token(request)
        return bool(provided) and hmac.compare_digest(provided, expected)

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        if request.path.startswith("/api/admin/"):
            if not self._expected_token():
                return web.json_response(
                    {
                        "success": False,
                        "error": "ADMIN_TOKEN 未配置：请在容器环境变量中设置后重启",
                    },
                    status=503,
                )
            if not self._authorized(request):
                return web.json_response(
                    {"success": False, "error": "unauthorized"}, status=401
                )
        return await handler(request)

    # ---- registration ----

    def register(self, app: web.Application) -> None:
        app.router.add_get("/admin", self.handle_admin_page)
        app.router.add_get("/api/admin/overview", self.handle_overview)
        app.router.add_get("/api/admin/config", self.handle_get_config)
        app.router.add_put("/api/admin/config", self.handle_put_config)
        app.router.add_post("/api/admin/config/test", self.handle_test_config)
        app.router.add_get("/api/admin/logs", self.handle_get_logs)
        app.router.add_post("/api/admin/logs/level", self.handle_log_level)
        logger.info("[AdminAPI] Admin panel routes registered (/admin)")

    # ---- page ----

    async def handle_admin_page(self, request: web.Request) -> web.Response:
        index_path = os.path.join(self.static_dir, "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            return web.Response(
                text="admin panel assets missing", status=500, content_type="text/plain"
            )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    # ---- overview ----

    async def handle_overview(self, request: web.Request) -> web.Response:
        data: dict[str, Any] = {
            "app": self._app_status(),
            "speaker": await self._speaker_status(),
            "backends": self._backends_status(),
            "audio": self._audio_status(),
            "external_services": await self._probe_external_services(),
            "runtime": {
                "config_path": str(self.config.get_config_path()),
                "overrides_path": str(runtime_overrides.path),
                "overrides_keys": self._override_leaf_count(),
                "log_level": logging_level_name(),
                "uptime_s": self._uptime_seconds(),
            },
        }
        return web.json_response({"success": True, "data": data})

    def _app_status(self) -> dict[str, Any]:
        app = get_app()
        state = getattr(app, "device_state", None) if app else None
        state_value = getattr(state, "value", state)
        return {
            "running": app is not None,
            "device_state": str(state_value) if state_value is not None else None,
            "xiaozhi_enabled": bool(getattr(app, "_enable_xiaozhi", False)) if app else False,
            "native_ready": True,
        }

    def _uptime_seconds(self) -> int:
        app = get_app()
        anchor = getattr(app, "started_at", None) or self.started_at
        return max(0, int(time.time() - anchor))

    @staticmethod
    async def _speaker_status() -> dict[str, Any]:
        from core.ref import get_speaker

        speaker = get_speaker()
        playing = None
        if speaker is not None:
            try:
                playing = await asyncio.wait_for(speaker.get_playing(), timeout=3)
            except Exception as exc:
                logger.debug(f"[AdminAPI] get_playing failed: {exc}")
                playing = None
        return {"ready": speaker is not None, "playing": playing}

    def _backends_status(self) -> dict[str, Any]:
        app = get_app()

        openai_cfg = self.config.get_app_config("openai", {}) or {}
        openai_status = {
            "enabled": safe_is_enabled(OpenAIManager),
            "connected": safe_is_connected(OpenAIManager),
            "base_url": getattr(OpenAIManager, "_base_url", None),
            "api_style": getattr(OpenAIManager, "_api_style", None),
            "model": getattr(OpenAIManager, "_model", None),
            "session_key": getattr(OpenAIManager, "_session_key", None),
            "has_key": bool(getattr(OpenAIManager, "_api_key", "")),
            "last_error": getattr(OpenAIManager, "last_error", None),
        }

        openclaw_cfg = self.config.get_app_config("openclaw", {}) or {}
        openclaw_status = {
            "enabled": safe_is_enabled(OpenClawManager),
            "connected": safe_is_connected(OpenClawManager),
            "url": getattr(OpenClawManager, "_url", None) or openclaw_cfg.get("url"),
            "last_error": getattr(OpenClawManager, "last_error", None),
        }

        qwenpaw_cfg = self.config.get_app_config("qwenpaw", {}) or {}
        qwenpaw_status = {
            "enabled": safe_is_enabled(QwenPawManager),
            "connected": safe_is_connected(QwenPawManager),
            "base_url": getattr(QwenPawManager, "_base_url", None) or qwenpaw_cfg.get("base_url"),
            "last_error": getattr(QwenPawManager, "last_error", None),
        }

        xiaozhi = get_xiaoai()
        xiaozhi_connected = False
        if xiaozhi is not None:
            try:
                xiaozhi_connected = bool(xiaozhi.is_connected())
            except Exception:
                xiaozhi_connected = False

        return {
            "openai": openai_status,
            "openclaw": openclaw_status,
            "qwenpaw": qwenpaw_status,
            "xiaozhi": {
                "enabled": bool(app and getattr(app, "_enable_xiaozhi", False)),
                "connected": xiaozhi_connected,
            },
        }

    def _audio_status(self) -> dict[str, Any]:
        vad = get_vad()
        kws = get_kws()
        asr_cfg = self.config.get_app_config("asr", {}) or {}
        return {
            "vad_present": vad is not None,
            "vad_paused": bool(getattr(vad, "paused", True)) if vad else None,
            "kws_present": kws is not None,
            "asr_model": asr_cfg.get("model"),
        }

    @staticmethod
    def _monitor_service_specs() -> list[dict[str, Any]]:
        """解析 MONITOR_SERVICES 环境变量（JSON 数组）。

        每项: {"name": "my-service", "url": "http://127.0.0.1:<port>/v1/models",
               "auth": "Bearer david"(可选)}
        未配置时返回空列表（面板隐藏该区块）。
        """
        raw = os.environ.get("MONITOR_SERVICES", "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[AdminAPI] MONITOR_SERVICES is not valid JSON, ignored")
            return []
        if not isinstance(parsed, list):
            return []
        specs = []
        for item in parsed:
            if isinstance(item, dict) and item.get("name") and item.get("url"):
                specs.append(
                    {
                        "name": str(item["name"]),
                        "url": str(item["url"]),
                        "auth": str(item.get("auth") or ""),
                    }
                )
        return specs

    async def _probe_external_services(self) -> list[dict[str, Any]]:
        """并发探测外部服务（MONITOR_SERVICES 声明的任意 HTTP 端点），单服务 4s 超时。"""
        specs = self._monitor_service_specs()
        if not specs:
            return []

        async def _probe(spec: dict[str, Any]) -> dict[str, Any]:
            headers = {}
            if spec["auth"]:
                auth = spec["auth"]
                headers["Authorization"] = (
                    auth if auth.lower().startswith("bearer ")
                    else f"Bearer {auth}"
                )
            started = time.monotonic()
            result = {
                "name": spec["name"],
                "url": spec["url"],
                "ok": False,
                "status": None,
                "latency_ms": None,
                "error": None,
            }
            try:
                timeout = aiohttp.ClientTimeout(total=4)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        spec["url"], headers=headers
                    ) as resp:
                        result["ok"] = resp.status < 500
                        result["status"] = resp.status
            except asyncio.TimeoutError:
                result["error"] = "timeout"
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                result["latency_ms"] = int((time.monotonic() - started) * 1000)
            return result

        return list(await asyncio.gather(*[_probe(s) for s in specs]))

    def _override_leaf_count(self) -> int:
        count = 0

        def _count(node: dict[str, Any]) -> None:
            nonlocal count
            for value in node.values():
                if isinstance(value, dict):
                    _count(value)
                else:
                    count += 1

        _count(runtime_overrides.snapshot())
        return count

    # ---- config ----

    async def handle_get_config(self, request: web.Request) -> web.Response:
        paths = list(_FIELD_INDEX.keys())
        values = _current_effective_values(paths)

        fields_out: list[dict[str, Any]] = []
        for field in CONFIG_SCHEMA:
            section = {**field}
            rendered = []
            for spec in section["fields"]:
                item = dict(spec)
                value = values.get(spec["path"])
                if spec["type"] == "secret":
                    item["value"] = _mask_secret(value)
                else:
                    item["value"] = value
                item["overridden"] = runtime_overrides.contains(spec["path"])
                rendered.append(item)
            section["fields"] = rendered
            fields_out.append(section)

        return web.json_response(
            {
                "success": True,
                "data": {
                    "schema": fields_out,
                    "overrides_path": str(runtime_overrides.path),
                },
            }
        )

    async def handle_put_config(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

        patch_raw = body.get("patch") if isinstance(body, dict) else None
        if patch_raw is None:
            return web.json_response(
                {"success": False, "error": "Missing required field: patch"}, status=400
            )

        try:
            patch = _sanitize_patch(patch_raw)
        except ValueError as exc:
            return web.json_response({"success": False, "error": str(exc)}, status=400)

        applied_paths = _flatten_leaves(patch)
        if applied_paths:
            runtime_overrides.update(patch)
            # 立即热重载：ConfigManager listeners（各后端 Manager）同步刷新，
            # 文件 watcher 的 1s 轮询也会兜底。
            self.config.reload_app_config()
            logger.info(
                f"[AdminAPI] Runtime overrides updated via panel: {', '.join(applied_paths)}"
            )

        values = _current_effective_values(list(_FIELD_INDEX.keys()))
        return web.json_response(
            {
                "success": True,
                "data": {
                    "applied": applied_paths,
                    "values": {
                        p: (
                            _mask_secret(v)
                            if _FIELD_INDEX[p]["type"] == "secret"
                            else v
                        )
                        for p, v in values.items()
                    },
                },
            }
        )

    # ---- connectivity test ----

    async def handle_test_config(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}

        backend = (body or {}).get("backend", "openai")
        if backend != "openai":
            return web.json_response(
                {"success": False, "error": f"unsupported backend: {backend}"}, status=400
            )

        cfg = self.config.get_app_config("openai", {}) or {}
        base_url = str((body or {}).get("base_url") or cfg.get("base_url") or "").rstrip("/")
        api_key = (body or {}).get("api_key")
        if api_key is None or api_key == "":
            # 未填新 key：用当前生效值测试（掩码无法回传明文）
            api_key = cfg.get("api_key") or ""
        model = str((body or {}).get("model") or cfg.get("model") or "")
        style = str(
            (body or {}).get("style") or cfg.get("api_style") or "chat_completions"
        ).strip()
        if style not in ("chat_completions", "openai_responses", "anthropic_messages"):
            return web.json_response(
                {"success": False, "error": f"unsupported style: {style}"}, status=400
            )

        if not base_url:
            return web.json_response(
                {"success": False, "error": "base_url 为空，无法测试"}, status=400
            )

        # 用「本次提交的待测 Key」构造探测头——不能复用 OpenAIManager._headers()，
        # 那会带上当前生效配置的旧 Key，导致换 Key 场景预检结果失真
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            if style == "anthropic_messages":
                headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"

        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=15)

        # 各规格的对话预检请求（max_tokens 压到最小）
        if style == "anthropic_messages":
            probe_url = (
                base_url if base_url.endswith("/messages")
                else f"{base_url}/messages"
            )
            probe_payload: dict[str, Any] = {
                "model": model or "claude-3-5-haiku-latest",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
        elif style == "openai_responses":
            probe_url = (
                base_url if base_url.endswith("/responses")
                else f"{base_url}/responses"
            )
            probe_payload = {
                "model": model or "gpt-4o-mini",
                "input": [{"role": "user", "content": "ping"}],
                "max_output_tokens": 16,
                "stream": False,
            }
        else:
            probe_url = (
                base_url if base_url.endswith("/chat/completions")
                else f"{base_url}/chat/completions"
            )
            probe_payload = {
                "model": model or "gpt-4o-mini",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }

        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 第一优先（Anthropic 除外）：GET /models 轻探测
            first_error = None
            if style != "anthropic_messages":
                try:
                    async with session.get(f"{base_url}/models", headers=headers) as resp:
                        latency_ms = int((time.monotonic() - started) * 1000)
                        if resp.status < 400:
                            return web.json_response(
                                {
                                    "success": True,
                                    "data": {
                                        "ok": True,
                                        "via": f"GET /models ({style})",
                                        "status": resp.status,
                                        "latency_ms": latency_ms,
                                        "base_url": base_url,
                                        "style": style,
                                    },
                                }
                            )
                        first_error = f"HTTP {resp.status} via GET /models"
                except asyncio.TimeoutError:
                    first_error = "timeout via GET /models"
                except aiohttp.ClientError as exc:
                    first_error = f"{type(exc).__name__}: {exc}"

            # 兜底：按接口规格发一次最小对话请求
            try:
                probe_started = time.monotonic()
                async with session.post(
                    probe_url, json=probe_payload, headers=headers
                ) as resp:
                    latency_ms = int((time.monotonic() - probe_started) * 1000)
                    body_text = await resp.text()
                    ok = resp.status < 400
                    return web.json_response(
                        {
                            "success": True,
                            "data": {
                                "ok": ok,
                                "via": f"POST {probe_url.rsplit('/', 1)[-1]}",
                                "status": resp.status,
                                "latency_ms": latency_ms,
                                "base_url": base_url,
                                "style": style,
                                "first_attempt_error": first_error if not ok else None,
                                "error": None if ok else body_text[:300],
                            },
                        }
                    )
            except asyncio.TimeoutError:
                return web.json_response(
                    {
                        "success": True,
                        "data": {
                            "ok": False,
                            "via": "POST",
                            "latency_ms": int((time.monotonic() - started) * 1000),
                            "base_url": base_url,
                            "style": style,
                            "first_attempt_error": first_error,
                            "error": "timeout",
                        },
                    }
                )
            except aiohttp.ClientError as exc:
                return web.json_response(
                    {
                        "success": True,
                        "data": {
                            "ok": False,
                            "via": "POST",
                            "latency_ms": int((time.monotonic() - started) * 1000),
                            "base_url": base_url,
                            "style": style,
                            "first_attempt_error": first_error,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    }
                )

    # ---- logs ----

    async def handle_get_logs(self, request: web.Request) -> web.Response:
        try:
            after = int(request.query.get("after", "-1"))
        except ValueError:
            after = -1
        limit = min(2000, max(1, int(request.query.get("limit", "500"))))
        entries, latest = get_memory_log_handler().get_after(after=after, limit=limit)
        return web.json_response(
            {
                "success": True,
                "data": {
                    "entries": entries,
                    "latest_seq": latest,
                    "dropped_total": get_memory_log_handler().dropped,
                },
            }
        )

    async def handle_log_level(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)

        level_name = str((body or {}).get("level", "")).upper()
        if level_name not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            return web.json_response(
                {"success": False, "error": f"unsupported level: {level_name}"}, status=400
            )
        numeric = getattr(logging, level_name)

        set_runtime_log_level(level_name, numeric)
        logger.info(f"[AdminAPI] Log level changed to {level_name}")
        return web.json_response({"success": True, "data": {"level": level_name}})


# ----------------------------------------------------------------- utils ----
def safe_is_enabled(manager_cls) -> bool:
    try:
        return bool(manager_cls.is_enabled())
    except Exception:
        return False


def safe_is_connected(manager_cls) -> bool:
    try:
        return bool(manager_cls.is_connected())
    except Exception:
        return False


def _flatten_leaves(node: dict[str, Any], prefix: str = "") -> list[str]:
    leaves: list[str] = []
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            leaves.extend(_flatten_leaves(value, prefix=f"{dotted}."))
        else:
            leaves.append(dotted)
    return leaves


def logging_level_name() -> str:
    import logging

    return logging.getLevelName(logging.getLogger("xiaozhi").getEffectiveLevel())


def set_runtime_log_level(level_name: str, numeric: int) -> None:
    import logging

    log = logging.getLogger("xiaozhi")
    log.setLevel(numeric)
    for handler in log.handlers:
        # 控制台 handler 保持与主级别一致；内存 handler 收全部便于排查
        if type(handler).__name__ != "MemoryLogHandler":
            handler.setLevel(numeric)
