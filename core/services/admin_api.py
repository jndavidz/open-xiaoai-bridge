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
import core.utils.backend_presets as backend_presets_mod
from core.utils.backend_presets import BackendPresets, backend_presets, mask_secret
from core.utils.runtime_overrides import runtime_overrides

# ---------------------------------------------------------------- schema ----
# 可通过面板编辑的白名单字段。type: string | int | float | bool | secret | select
# secret 字段读取时只回掩码，保存留空=不修改，null=清除覆盖回落底层值。
CONFIG_SCHEMA: list[dict[str, Any]] = [
    # 「AI 对话后端」的地址/规格/模型/Key 已由预设条（/api/admin/presets）接管，
    # 不再开放裸表单——切换预设即写入覆盖层，日常无需直接编辑。
    # 仅保留 response_timeout 供微调；「清除覆盖」由独立端点提供（见 clear_overrides）。
    {
        "id": "openai_advanced",
        "title": "AI 对话后端 · 高级",
        "description": "预设条之上的兜底项：响应超时可微调；「清除覆盖」会把地址/规格/模型/Key 的面板覆盖全部清空，回落 config.py / 环境变量注入的原始值（预设不受影响）。",
        "fields": [
            {
                "path": "openai.response_timeout",
                "label": "响应超时（秒）",
                "type": "int",
                "help": "对话请求的总超时；测速与预检另有各自更短的超时，互不影响",
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

# ---------------------------------------------------------- 测速默认参数 ----
# 测速只衡量「对话场景」：发一次真实对话请求，量端到端响应耗时 + 生成速度。
# 默认多轮取最后 1 轮计入统计——首轮含 TLS/DNS 建连与服务端冷启动，
# 不代表稳态体感，故前几轮只作预热。
BENCH_DEFAULT_ROUNDS = 3
BENCH_MAX_ROUNDS = 10
BENCH_DEFAULT_MAX_TOKENS = 300
BENCH_MAX_MAX_TOKENS = 2048
BENCH_TIMEOUT_S = 60
# 失败轮次的排序惩罚：失败项总分 = 惩罚 × 轮数，必定排在成功项之后
BENCH_FAILURE_PENALTY_MS = 100_000
BENCH_DEFAULT_PROMPT = "用三句话介绍你自己，并说明你能帮我做什么。"


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


def _build_probe_headers(style: str, api_key: str) -> dict[str, str]:
    """按接口规格构造鉴权头。

    必须用「本次提交的待测 Key」——不能复用 OpenAIManager._headers()，
    那会带上当前生效配置的旧 Key，导致换 Key 场景预检结果失真。
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if style == "anthropic_messages":
            # Anthropic 官方鉴权头；同时保留 Bearer 兼容各类代理网关
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
    return headers


def _build_probe_request(
    style: str, base_url: str, model: str, *, prompt: str, max_tokens: int
) -> tuple[str, dict[str, Any]]:
    """构造一次对话探测请求（端点 URL + payload），max_tokens 由调用方压控。"""
    base_url = base_url.rstrip("/")
    if style == "anthropic_messages":
        probe_url = base_url if base_url.endswith("/messages") else f"{base_url}/messages"
        payload: dict[str, Any] = {
            "model": model or "claude-3-5-haiku-latest",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        }
    elif style == "openai_responses":
        probe_url = base_url if base_url.endswith("/responses") else f"{base_url}/responses"
        payload = {
            "model": model or "gpt-4o-mini",
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": max_tokens,
            "stream": False,
        }
    else:
        probe_url = (
            base_url
            if base_url.endswith("/chat/completions")
            else f"{base_url}/chat/completions"
        )
        payload = {
            "model": model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        }
    return probe_url, payload


def _extract_probe_text(style: str, body: Any) -> str:
    """从探测响应中取文本，用于测速统计输出量（复用 OpenAIManager 的解析语义）。"""
    from core.openai import OpenAIManager

    if style == "anthropic_messages":
        return OpenAIManager._extract_anthropic_text(body) or ""
    if style == "openai_responses":
        return OpenAIManager._extract_responses_text(body) or ""
    return OpenAIManager._extract_response_text(body) or ""


def _count_tokens(body: Any, text: str) -> int | None:
    """取上游自报的 completion tokens；缺失返回 None（前端显示 —）。"""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    for key in ("completion_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


# --------------------------------------------------------------- handlers ----
class AdminAPI:
    """Admin 路由注册中心与处理器集合。"""

    def __init__(self, static_dir: Optional[str] = None, presets: BackendPresets | None = None):
        self.config = ConfigManager.instance()
        if static_dir is None:
            static_dir = os.path.join(
                os.path.dirname(__file__), "admin_static"
            )
        self.static_dir = static_dir
        # 预设库：默认用模块级单例；测试可注入独立实例（指向临时目录）
        self.presets = presets if presets is not None else backend_presets
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
        # AI 对话后端预设库（一键切换 / 测速）
        app.router.add_get("/api/admin/presets", self.handle_list_presets)
        app.router.add_post("/api/admin/presets", self.handle_create_preset)
        app.router.add_put("/api/admin/presets", self.handle_reorder_presets)
        app.router.add_put("/api/admin/presets/{pid}", self.handle_update_preset)
        app.router.add_delete("/api/admin/presets/{pid}", self.handle_delete_preset)
        app.router.add_post("/api/admin/presets/{pid}/switch", self.handle_switch_preset)
        app.router.add_post("/api/admin/presets/benchmark", self.handle_benchmark)
        app.router.add_post("/api/admin/presets/clear-overrides", self.handle_clear_overrides)
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
        headers = _build_probe_headers(style, str(api_key))

        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=15)

        # 各规格的对话预检请求（max_tokens 压到最小，只验连通不看内容）
        probe_url, probe_payload = _build_probe_request(
            style, base_url, model, prompt="ping", max_tokens=1
        )

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

    # ---- AI 对话后端预设（一键切换） ----

    async def handle_list_presets(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "data": {
                    "presets": self.presets.all(),
                    "path": str(self.presets.path),
                    "styles": list(backend_presets_mod.API_STYLES),
                },
            }
        )

    async def handle_create_preset(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"success": False, "error": "body must be an object"}, status=400)

        # 复制现有按钮：服务端从源预设取明文（含 api_key），绕开「列表只给掩码」的限制
        source_id = str(body.get("copy_from") or "").strip()
        if source_id:
            source = self.presets.get_raw(source_id)
            if source is None:
                return web.json_response(
                    {"success": False, "error": f"copy_from preset not found: {source_id}"},
                    status=404,
                )
            draft = {k: v for k, v in source.items() if k not in ("id", "name")}
            name = str(body.get("name") or "").strip()
            draft["name"] = name or f"{source.get('name')} 副本"
            body = {**draft, **{k: v for k, v in body.items() if k in ("name",)}}

        preset = self.presets.add(body)
        logger.info(f"[AdminAPI] Backend preset created: {preset['name']} ({preset['id']})"
                    + (f" (copied from {source_id})" if source_id else ""))
        return web.json_response({"success": True, "data": {"preset": preset}})

    async def handle_reorder_presets(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        ids = (body or {}).get("ids")
        if not isinstance(ids, list):
            return web.json_response(
                {"success": False, "error": "Missing required field: ids (array)"}, status=400
            )
        return web.json_response(
            {"success": True, "data": {"presets": self.presets.reorder([str(i) for i in ids])}}
        )

    async def handle_update_preset(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"success": False, "error": "body must be an object"}, status=400)
        updated = self.presets.update(pid, body)
        if updated is None:
            return web.json_response({"success": False, "error": "preset not found"}, status=404)
        return web.json_response({"success": True, "data": {"preset": updated}})

    async def handle_delete_preset(self, request: web.Request) -> web.Response:
        pid = request.match_info["pid"]
        if not self.presets.delete(pid):
            return web.json_response({"success": False, "error": "preset not found"}, status=404)
        logger.info(f"[AdminAPI] Backend preset deleted: {pid}")
        return web.json_response({"success": True, "data": {"deleted": pid}})

    async def handle_switch_preset(self, request: web.Request) -> web.Response:
        """把某条预设写入覆盖层并热生效（一键切换）。

        预设可能缺 api_key（如本地 ollama），故按「预设里实际存在的字段」展开 patch：
        不写 null，避免把用户没填的字段清成空（null 在覆盖层 = 删除回落底层值）。
        """
        pid = request.match_info["pid"]
        preset = self.presets.get_raw(pid)
        if preset is None:
            return web.json_response({"success": False, "error": "preset not found"}, status=404)

        patch = self.presets.to_patch(preset)
        if not patch:
            return web.json_response(
                {"success": False, "error": "预设内容为空，无可切换字段"}, status=400
            )

        applied = _flatten_leaves(patch)
        runtime_overrides.update(patch)
        # 立即热重载：ConfigManager listeners（OpenAIManager）同步刷新
        self.config.reload_app_config()
        logger.info(
            f"[AdminAPI] Switched AI backend to preset {preset.get('name')} "
            f"({pid}): {', '.join(applied)}"
        )

        values = _current_effective_values(list(_FIELD_INDEX.keys()))
        return web.json_response(
            {
                "success": True,
                "data": {
                    "applied": applied,
                    "preset": {k: v for k, v in preset.items() if k != "api_key"},
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

    # ---- 测速（对话场景：非流式端到端） ----

    async def handle_clear_overrides(self, request: web.Request) -> web.Response:
        """清除地址/规格/模型/Key 的面板覆盖，回落 config.py / 环境变量原始值。

        逃生舱：预设库接管日常切换后，用户可能把覆盖层改得面目全非（如误填了
        不可达地址），需要一条不依赖任何预设的「回到出厂」通道。response_timeout
        与其它 section 的覆盖不受影响；预设列表原样保留。
        """
        from core.utils.backend_presets import FIELD_PATHS

        cleared: list[str] = []
        for dotted in FIELD_PATHS.values():
            if runtime_overrides.contains(dotted):
                runtime_overrides.update(path_to_override(dotted, None))
                cleared.append(dotted)
        if cleared:
            self.config.reload_app_config()
            logger.info(f"[AdminAPI] Cleared openai overrides: {', '.join(cleared)}")

        values = _current_effective_values(
            ["openai.base_url", "openai.api_style", "openai.model",
             "openai.api_key", "openai.response_timeout"]
        )
        return web.json_response(
            {
                "success": True,
                "data": {
                    "cleared": cleared,
                    "values": {
                        p: (
                            mask_secret(v) if p == "openai.api_key" else v
                        )
                        for p, v in values.items()
                    },
                },
            }
        )

    async def handle_benchmark(self, request: web.Request) -> web.Response:
        """对一组候选后端做对话场景测速排序。

        请求体：
            {
              "ids": ["a", "b"],          // 留空/缺省 = 全部预设
              "prompt": "…",             // 可选
              "rounds": 3,               // 每候选轮数（最后 1 轮计入统计）
              "max_tokens": 300          // 可选
            }

        统计口径（对话体感）：
            - ok_rounds / rounds：成功轮数
            - avg_ms / min_ms：端到端耗时（取成功轮）
            - tokens：最后一轮的 completion tokens（上游自报，缺失为 null）
            - score：avg_ms + 失败惩罚(BENCH_FAILURE_PENALTY_MS)；纯失败项排最后
        """
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        body = body if isinstance(body, dict) else {}

        raw_ids = body.get("ids")
        if raw_ids is None:
            targets = self.presets.all(mask=False)
        else:
            if not isinstance(raw_ids, list):
                return web.json_response(
                    {"success": False, "error": "ids must be an array"}, status=400
                )
            targets = [
                p for p in (self.presets.get_raw(str(i)) for i in raw_ids) if p is not None
            ]
        if not targets:
            return web.json_response(
                {"success": True, "data": {"results": [], "config": {}}}
            )

        try:
            rounds = int(body.get("rounds", BENCH_DEFAULT_ROUNDS))
        except (TypeError, ValueError):
            rounds = BENCH_DEFAULT_ROUNDS
        # 首轮预热：rounds 为实际发起的请求数，其中第 1 轮不计入统计
        rounds = max(1, min(BENCH_MAX_ROUNDS, rounds))

        try:
            max_tokens = int(body.get("max_tokens", BENCH_DEFAULT_MAX_TOKENS))
        except (TypeError, ValueError):
            max_tokens = BENCH_DEFAULT_MAX_TOKENS
        max_tokens = max(16, min(BENCH_MAX_MAX_TOKENS, max_tokens))

        prompt = str(body.get("prompt") or BENCH_DEFAULT_PROMPT).strip() or BENCH_DEFAULT_PROMPT

        # 并发测各候选（候选之间并行；同一候选的多轮内部串行，保证预热顺序）
        results = await asyncio.gather(
            *[self._benchmark_one(p, rounds, prompt, max_tokens) for p in targets]
        )

        ranked = sorted(
            results,
            key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0),
        )
        return web.json_response(
            {
                "success": True,
                "data": {
                    "results": ranked,
                    "config": {
                        "rounds": rounds,
                        "prompt": prompt,
                        "max_tokens": max_tokens,
                        "failure_penalty_ms": BENCH_FAILURE_PENALTY_MS,
                    },
                },
            }
        )

    async def _benchmark_one(
        self, preset: dict[str, Any], rounds: int, prompt: str, max_tokens: int
    ) -> dict[str, Any]:
        """对单个预设跑 rounds 轮对话测速，返回聚合结果。"""
        base_url = str(preset.get("base_url") or "").strip()
        style = str(preset.get("api_style") or "chat_completions").strip()
        model = str(preset.get("model") or "")
        api_key = str(preset.get("api_key") or "")

        result: dict[str, Any] = {
            "id": preset.get("id"),
            "name": preset.get("name"),
            "model": model,
            "base_url": base_url,
            "api_style": style,
            "rounds": rounds,
            "ok": False,
            "ok_rounds": 0,
            "avg_ms": None,
            "min_ms": None,
            "last_ms": None,
            "tokens": None,
            "chars": None,
            "score": None,
            "error": None,
            "preview": None,
        }

        if not base_url:
            result["error"] = "base_url 为空"
            result["score"] = BENCH_FAILURE_PENALTY_MS * rounds
            return result

        headers = _build_probe_headers(style, api_key)
        probe_url, payload = _build_probe_request(
            style, base_url, model, prompt=prompt, max_tokens=max_tokens
        )

        durations: list[int] = []
        last_error: str | None = None
        last_tokens: int | None = None
        last_chars: int | None = None
        preview: str | None = None

        timeout = aiohttp.ClientTimeout(total=BENCH_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for round_index in range(rounds):
                started = time.monotonic()
                try:
                    async with session.post(probe_url, json=payload, headers=headers) as resp:
                        body = await resp.json(content_type=None)
                        elapsed = int((time.monotonic() - started) * 1000)
                        if resp.status >= 400:
                            message = body.get("error", body) if isinstance(body, dict) else body
                            last_error = f"HTTP {resp.status}: {str(message)[:200]}"
                            continue
                        # 首轮作预热：含 TLS/DNS 建连与服务端冷启动，不代表稳态体感，
                        # 不计入统计（rounds=1 时无预热，仅测这一轮）
                        if round_index > 0 or rounds == 1:
                            durations.append(elapsed)
                        text = _extract_probe_text(style, body)
                        last_tokens = _count_tokens(body, text)
                        last_chars = len(text) if text else 0
                        preview = text[:120] if text else None
                except asyncio.TimeoutError:
                    last_error = f"timeout（>{BENCH_TIMEOUT_S}s）"
                except aiohttp.ClientError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                except Exception as exc:  # 解析等意外，不中断其余候选
                    last_error = f"{type(exc).__name__}: {exc}"

        scored_rounds = max(1, rounds - 1) if rounds > 1 else 1
        result["ok_rounds"] = len(durations)
        result["rounds"] = scored_rounds          # 语义改为「计入统计的轮数」
        result["total_rounds"] = rounds           # 实际发起的请求数（含预热）
        result["last_ms"] = durations[-1] if durations else None
        result["tokens"] = last_tokens
        result["chars"] = last_chars
        result["preview"] = preview
        if durations:
            result["ok"] = True
            result["avg_ms"] = int(sum(durations) / len(durations))
            result["min_ms"] = min(durations)
            failed = scored_rounds - len(durations)
            # 部分失败只惩罚失败轮次，仍能参与排序（否则 3 轮挂 1 轮就被挤到末尾）
            result["score"] = result["avg_ms"] + failed * BENCH_FAILURE_PENALTY_MS
        else:
            result["score"] = BENCH_FAILURE_PENALTY_MS * scored_rounds
        result["error"] = (
            last_error if not durations
            else (last_error if len(durations) < scored_rounds else None)
        )
        return result

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


def path_to_override(dotted: str, value: Any) -> dict[str, Any]:
    """把点分路径包成覆盖层 patch（{"openai": {"model": value}}）。

    value=None 语义 = 覆盖层删除该键、回落底层值（供「清除覆盖」使用）。
    """
    parts = dotted.split(".")
    root: dict[str, Any] = {}
    node: dict[str, Any] = root
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            node[part] = value
        else:
            node = node.setdefault(part, {})
    return root


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
