"""业务 API 鉴权（纯函数，不 import aiohttp / core 其它模块，便于单测）。

边界：
    - 路径分类（health / read / write）
    - `Authorization: Bearer <token>` 头解析
    - 判定：返回 `AuthDecision`（放行 / 401 / 403 / 503）
    aiohttp 适配（中间件、json_response）留在 `api_server.py`，本模块只做纯逻辑。

安全语义（ticket 06）：
    - 配置 `BRIDGE_API_TOKEN` 时，写类端点必须带 `Authorization: Bearer <token>`；
      缺失/格式错误 401，token 不匹配 403。
    - **未配置时写类端点失败关闭（503）**，响应里给出配置提示；health/read 仍可用。
    - 管理面板 token（`ADMIN_TOKEN`）与本模块完全分离：这里只读 `BRIDGE_API_TOKEN`，
      不因为配了 ADMIN_TOKEN 就解除业务端点的关闭状态，也不回退。
    - 错误文案与日志不含 token 明文，也不含服务端内部路径。
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass

# 业务 API token 的环境变量名（与管理面板的 ADMIN_TOKEN 分离）
ENV_TOKEN = "BRIDGE_API_TOKEN"

KIND_HEALTH = "health"
KIND_READ = "read"
KIND_WRITE = "write"

# 健康检查：永远免鉴权，但处理器不得返回凭证/完整文本/设备敏感信息。
HEALTH_ROUTES = frozenset({"/api/health"})

# 只读端点：安全方法（GET/HEAD/OPTIONS）时免鉴权。
# ⚠️ `/api/inject/text`（识别文本 WS 订阅）**不在**此列——它推送语音内容，
# 属敏感读，按写类处理（要 token）；否则未配置 token 时局域网可旁听语音文本。
READ_ROUTES = frozenset({
    "/api/status",          # 播放状态（playing/paused/idle）
    "/api/inject",          # 注入闸门状态（只回 open/closed + 剩余秒数）
    "/api/inject/relay",    # 中继计数（字节数/队列长度）
    "/api/tts/doubao_voices",
})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 未配置 token 时的固定提示：只给变量名与调用方式，不给内部路径
UNCONFIGURED_ERROR = (
    "BRIDGE_API_TOKEN 未配置，写类接口已失败关闭（fail closed）。"
    "请在服务端环境变量设置 BRIDGE_API_TOKEN 并重启，"
    "再以 Authorization: Bearer <token> 调用。"
)
# 401/403 文案保持简短且与既有 API 风格一致（不提示任何服务端细节）
UNAUTHORIZED_ERROR = "unauthorized"
FORBIDDEN_ERROR = "forbidden"

_BEARER_HEADER = ("WWW-Authenticate", "Bearer")


@dataclass(frozen=True)
class AuthDecision:
    """一次鉴权判定的结果（中间件据此构造 HTTP 响应）。"""

    allowed: bool
    status: int
    code: str
    error: str = ""
    headers: tuple[tuple[str, str], ...] = ()

    def header_dict(self) -> dict[str, str]:
        return dict(self.headers)


_ALLOW = AuthDecision(allowed=True, status=200, code="ok")


def _normalize_path(path: str) -> str:
    """去掉查询串与尾部斜杠（`/api/status/?x=1` → `/api/status`）。"""
    p = (path or "").split("?", 1)[0].strip()
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def classify_route(path: str, method: str = "GET") -> str:
    """把一次请求分类为 health / read / write。

    默认 **write**（失败关闭）：只有显式登记的只读端点 + 安全方法才算读类，
    未登记路径（含拼错路径、未来新增的写端点）一律要求鉴权——
    这样鉴权先于路由 404，既不误伤只读监控，也不泄露路由是否存在。
    """
    p = _normalize_path(path)
    m = (method or "GET").strip().upper()
    if m in SAFE_METHODS:
        if p in HEALTH_ROUTES:
            return KIND_HEALTH
        if p in READ_ROUTES:
            return KIND_READ
    return KIND_WRITE


def parse_bearer(header: str | None) -> str | None:
    """解析 `Authorization: Bearer <token>`；任何不合规形式都返回 None。

    严格两段式：必须恰好是「scheme + 凭据」，允许大小写差异与多余空白。
    `Bearer a b`、`Bearer`（空凭据）、`Basic ...` 全部拒绝。
    """
    if not header or not str(header).strip():
        return None
    parts = str(header).strip().split()
    if len(parts) != 2:
        return None
    scheme, credentials = parts
    if scheme.lower() != "bearer":
        return None
    credentials = credentials.strip()
    return credentials or None


def configured_token(environ: Mapping[str, str] | None = None) -> str:
    """读取业务 token（去空白）；未配置/空白 → 空串（调用方据此失败关闭）。"""
    env = os.environ if environ is None else environ
    return str(env.get(ENV_TOKEN, "") or "").strip()


def authorize(
    path: str,
    method: str = "GET",
    *,
    authorization: str | None = None,
    expected_token: str = "",
) -> AuthDecision:
    """唯一判定点：health/read 放行；write 需要匹配的 Bearer token。

    `expected_token` 为空串表示服务端未配置 token → 写类端点 503（失败关闭）。
    """
    if classify_route(path, method) in (KIND_HEALTH, KIND_READ):
        return _ALLOW
    if not expected_token:
        return AuthDecision(
            allowed=False,
            status=503,
            code="auth_unconfigured",
            error=UNCONFIGURED_ERROR,
        )
    provided = parse_bearer(authorization)
    if provided is None:
        return AuthDecision(
            allowed=False,
            status=401,
            code="token_missing",
            error=UNAUTHORIZED_ERROR,
            headers=(_BEARER_HEADER,),
        )
    # 常时比较（按 bytes：hmac.compare_digest 对非 ASCII str 会抛 TypeError）
    if not hmac.compare_digest(
        provided.encode("utf-8"), expected_token.encode("utf-8")
    ):
        return AuthDecision(
            allowed=False,
            status=403,
            code="token_invalid",
            error=FORBIDDEN_ERROR,
        )
    return _ALLOW
