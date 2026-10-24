#!/usr/bin/env python3
"""
OpenXiaoAI API 配置和基础工具

鉴权（ticket 09）：
    服务端配置 `BRIDGE_API_TOKEN` 后，写类端点（play/wakeup/interrupt/tts）一律
    要求 `Authorization: Bearer <token>`（未配置 token 时服务端 503 失败关闭）。
    本客户端从同名环境变量读取 token；**未配置时在发请求前就给出明确错误**，
    而不是等服务端回 401/503。

    只读端点（`/api/health`、`/api/status`、`/api/tts/doubao_voices`）不强制 token，
    但若环境里配了 token 也会带上（无害，便于排查与服务端口径一致）。
"""

import os
import json
import urllib.request
import urllib.error

# 业务 API token 环境变量（与后台面板的 ADMIN_TOKEN 分离）
TOKEN_ENV = "BRIDGE_API_TOKEN"

# 只读端点：安全方法下免鉴权（与服务端 auth.READ_ROUTES / HEALTH_ROUTES 对应）
READ_PATHS = frozenset({
    "/api/health",
    "/api/status",
    "/api/tts/doubao_voices",
})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

MISSING_TOKEN_ERROR = (
    f"{TOKEN_ENV} 未配置：写类接口（play/wakeup/interrupt/tts）需要鉴权。"
    f"请先 export {TOKEN_ENV}=<token> 再重试（客户端会以 "
    "Authorization: Bearer <token> 调用）。"
)


class ApiAuthError(Exception):
    """客户端侧鉴权前置错误（token 未配置 / 环境变量缺失）。"""


def get_api_config():
    """获取 API 配置"""
    base_url = os.environ.get("OPENXIAOAI_BASE_URL", "http://192.168.3.6:9092")
    # 移除末尾的斜杠
    return base_url.rstrip("/")


def get_token(environ=None):
    """读取业务 token（去空白）；未配置 → 空串。"""
    env = os.environ if environ is None else environ
    return str(env.get(TOKEN_ENV, "") or "").strip()


def require_token():
    """取 token，未配置时抛出带修复提示的 ApiAuthError。"""
    token = get_token()
    if not token:
        raise ApiAuthError(MISSING_TOKEN_ERROR)
    return token


def auth_headers(token=None):
    """构造鉴权头；token 为空返回空 dict（便于只读端点降级）。"""
    value = get_token() if token is None else str(token).strip()
    return {"Authorization": f"Bearer {value}"} if value else {}


def requires_auth(path, method="GET"):
    """判定一次请求是否需要 token（与服务端「未登记即按写类失败关闭」一致）。"""
    normalized = (path or "").split("?", 1)[0].rstrip("/")
    if (method or "GET").upper() in SAFE_METHODS and normalized in READ_PATHS:
        return False
    return True


def api_request(path, method="GET", data=None, headers=None, require_auth=None):
    """发送 API 请求

    `require_auth=None` 时按路径/方法自动判定；写类请求缺少 token 直接抛
    `ApiAuthError`（不发请求，错误信息里不含 token 值）。
    """
    base_url = get_api_config()
    full_url = f"{base_url}{path}"

    token = get_token()
    if require_auth is None:
        require_auth = requires_auth(path, method)
    if require_auth and not token:
        raise ApiAuthError(MISSING_TOKEN_ERROR)

    default_headers = {
        "Content-Type": "application/json"
    }
    default_headers.update(auth_headers(token))
    if headers:
        default_headers.update(headers)

    req = urllib.request.Request(
        full_url,
        headers=default_headers,
        method=method
    )

    if data:
        req.data = json.dumps(data).encode("utf-8")

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_msg = f"HTTP 错误: {e.code} - {e.reason}"
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            error_msg += f"\n详情: {error_body}"
        except Exception:
            pass
        raise Exception(error_msg)
    except Exception as e:
        raise Exception(f"请求失败: {e}")


def check_health():
    """检查服务健康状态"""
    return api_request("/api/health")


def get_status():
    """获取音箱状态"""
    return api_request("/api/status")


def wakeup(silent=True):
    """唤醒小爱"""
    return api_request("/api/wakeup", method="POST", data={"silent": silent})


def interrupt():
    """打断当前播放"""
    return api_request("/api/interrupt", method="POST")
