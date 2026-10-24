"""业务 API 鉴权（纯函数，不 import aiohttp / core 其它模块，便于单测）。

边界：
    - 路径分类（health / read / write）
    - `Authorization: Bearer <token>` 头解析
    - 判定：返回 `AuthDecision`（放行 / 401 / 403 / 503）
    aiohttp 适配（中间件、json_response）留在 `api_server.py`，本模块只做纯逻辑。

安全语义（ticket 06；多 token 见 ticket 12 / notes/10-token-model.md）：
    - 配置 token 时，写类端点必须带 `Authorization: Bearer <token>`；
      缺失/格式错误 401，token 不匹配 403。
    - token 来源两级：`BRIDGE_API_TOKENS=label:token,...`（具名集合，label 进审计日志）；
      `BRIDGE_API_TOKEN` 保留为单一 `legacy` 标签（迁移并存窗口，收口后删除）。
    - **未配置时写类端点失败关闭（503）**，响应里给出配置提示；health/read 仍可用。
    - **配置非法（条目格式错 / label 重复 / token 值重复）时整份判非法，同样 503
      （`auth_config_invalid`）**：半残配置会造成「某些调用方 403」的静默错觉，
      整份失败必然被发现。
    - 管理面板 token（`ADMIN_TOKEN`）与本模块完全分离：这里不读 ADMIN_TOKEN，
      不因为配了 ADMIN_TOKEN 就解除业务端点的关闭状态，也不回退。
    - 错误文案与日志不含 token 明文（连尾 N 位也不含），也不含服务端内部路径。
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# 业务 API token 的环境变量名（与管理面板的 ADMIN_TOKEN 分离）
ENV_TOKEN = "BRIDGE_API_TOKEN"
# 具名 token 集合（ticket 12）：`label:token` 逗号分隔
ENV_TOKENS = "BRIDGE_API_TOKENS"

# 旧共享值并入集合时使用的固定 label（收口 = 删除该 entry，勿改名保留）
LEGACY_LABEL = "legacy"

# label / token 的字符集约束（notes/10 §2.1）。hex 随机值天然满足 token 集合。
_LABEL_RE = re.compile(r"[a-z0-9-]{1,32}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9._~-]{32,128}")

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
    "BRIDGE_API_TOKENS 未配置（或仅有空的 BRIDGE_API_TOKEN），"
    "写类接口已失败关闭（fail closed）。"
    "请在服务端环境变量设置 BRIDGE_API_TOKENS（label:token 逗号分隔）或 BRIDGE_API_TOKEN 并重启，"
    "再以 Authorization: Bearer <token> 调用。"
)
# 配置非法时的固定提示：细节（label 与原因）只在服务端日志，不回给客户端
CONFIG_INVALID_ERROR = (
    "token 配置非法（BRIDGE_API_TOKENS / BRIDGE_API_TOKEN），"
    "写类接口已失败关闭（auth_config_invalid）。请检查服务端日志中的条目错误并修正后重启。"
)
# 401/403 文案保持简短且与既有 API 风格一致（不提示任何服务端细节）
UNAUTHORIZED_ERROR = "unauthorized"
FORBIDDEN_ERROR = "forbidden"

_BEARER_HEADER = ("WWW-Authenticate", "Bearer")


@dataclass(frozen=True)
class AuthDecision:
    """一次鉴权判定的结果（中间件据此构造 HTTP 响应）。

    `label` 是命中 entry 的具名标签：token 决定 ⇒ 不可伪造，审计用它回答
    「是谁」；`caller`（peer）回答「从哪来」，二者不等价（notes/10 §4）。
    """

    allowed: bool
    status: int
    code: str
    error: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    label: str = ""

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


@dataclass(frozen=True)
class TokenSet:
    """具名 token 集合：entries 为 (label, token) 对；errors 非空 = 整份配置非法。

    判定语义（fail closed）：
      - errors 非空 → 写类端点一律 503 `auth_config_invalid`（带不带 token 都拒绝）；
      - entries 为空 → 503 `auth_unconfigured`；
      - 二者皆无 → 逐 entry 常时比较，命中即放行并返回 label。
    """

    entries: tuple[tuple[str, str], ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(label for label, _ in self.entries)

    def match(self, provided: str) -> str:
        """返回命中的 label；不命中返回空串。

        对**所有** entry 逐一常时比较、不提前退出（≤5 项成本可忽略，
        换取不泄露「前缀像哪个 token」的用时侧信道）。
        """
        provided_b = provided.encode("utf-8")
        matched = ""
        for label, token in self.entries:
            if hmac.compare_digest(provided_b, token.encode("utf-8")):
                matched = label
        return matched


def parse_token_set(raw: str) -> TokenSet:
    """解析 `label:token,label:token,...`；任一条目非法 ⇒ 整份非法。

    规则（notes/10 §2.1）：
      - `label` 限 `[a-z0-9-]{1,32}`，`token` 限 `[A-Za-z0-9._~-]{32,128}`；
      - 条目必须形如 `label:token`（按**第一个**冒号切分，token 内不允许冒号——
        字符集本就不含冒号）；
      - label 重复 / token 值重复 ⇒ 非法；
      - 空条目（多余逗号/空白段）⇒ 非法；
      - 整串空白 → 空集合（视为未配置，而不是非法）。
    """
    text = str(raw or "").strip()
    if not text:
        return TokenSet()

    entries: list[tuple[str, str]] = []
    errors: list[str] = []
    seen_labels: dict[str, int] = {}
    seen_tokens: dict[str, str] = {}

    for index, segment in enumerate(text.split(","), start=1):
        seg = segment.strip()
        if not seg:
            errors.append(f"条目 {index}: 空条目")
            continue
        label, sep, token = seg.partition(":")
        label, token = label.strip(), token.strip()
        if not sep:
            errors.append(f"条目 {index}: 缺少 label:token 分隔符")
            continue
        if not _LABEL_RE.fullmatch(label):
            # 不回显 label 原文：误把 token 填进 label 位时，值不能进日志
            errors.append(f"条目 {index}: label 不合法（限 [a-z0-9-]{{1,32}}）")
            continue
        if not _TOKEN_RE.fullmatch(token):
            errors.append(f"条目 {index}（label={label}）: token 不合法"
                          f"（限 [A-Za-z0-9._~-]{{32,128}}）")
            continue
        if label in seen_labels:
            errors.append(f"条目 {index}: label 重复（{label}，条目 {seen_labels[label]}）")
            continue
        if token in seen_tokens:
            errors.append(
                f"条目 {index}（label={label}）: token 值与 "
                f"label={seen_tokens[token]} 的条目重复"
            )
            continue
        seen_labels[label] = index
        seen_tokens[token] = label
        entries.append((label, token))

    return TokenSet(entries=tuple(entries), errors=tuple(errors))


def _label_of(entries: list[tuple[str, str]], token: str) -> str:
    for label, tok in entries:
        if tok == token:
            return label
    return ""


def configured_tokens(environ: Mapping[str, str] | None = None) -> TokenSet:
    """读环境变量组装 token 集合（每个请求调用一次，与既有口径一致）。

    - `BRIDGE_API_TOKENS` 非空：严格解析；非法 → 原样返回（errors 非空）。
      解析合法时再把 `BRIDGE_API_TOKEN`（若有值）作为 `legacy` 合并：
      与既有 label 冲突或 token 值重复 ⇒ 整份非法。
    - 仅 `BRIDGE_API_TOKEN` 非空：单一 `("legacy", 值)`，**不做字符集校验**
      （旧值是在旧规则下设置的，合并窗口不因旧值格式而失败；
      「只配 BRIDGE_API_TOKEN」时行为与多 token 之前逐字节一致）。
    - 都为空/空白 → 空集合（未配置，写类 503）。
    """
    env = os.environ if environ is None else environ
    raw_tokens = str(env.get(ENV_TOKENS, "") or "").strip()
    raw_legacy = str(env.get(ENV_TOKEN, "") or "").strip()

    if not raw_tokens:
        if not raw_legacy:
            return TokenSet()
        return TokenSet(entries=((LEGACY_LABEL, raw_legacy),))

    token_set = parse_token_set(raw_tokens)
    if not token_set.valid:
        return token_set

    if raw_legacy:
        if LEGACY_LABEL in token_set.labels:
            return TokenSet(
                entries=token_set.entries,
                errors=(f"BRIDGE_API_TOKEN 的 label {LEGACY_LABEL} 与 "
                        f"{ENV_TOKENS} 中同名条目冲突",),
            )
        label_of_same_value = _label_of(list(token_set.entries), raw_legacy)
        if label_of_same_value:
            return TokenSet(
                entries=token_set.entries,
                errors=(f"BRIDGE_API_TOKEN 的 token 值与 {ENV_TOKENS} 中 "
                        f"label={label_of_same_value} 重复",),
            )
        return TokenSet(
            entries=token_set.entries + ((LEGACY_LABEL, raw_legacy),),
        )
    return token_set


def authorize(
    path: str,
    method: str = "GET",
    *,
    authorization: str | None = None,
    tokens: TokenSet | None = None,
) -> AuthDecision:
    """唯一判定点：health/read 放行；write 需要匹配集合中的 Bearer token。

    判定顺序（notes/10 §2.1）：
      health/read 放行 → 配置非法 503 `auth_config_invalid` →
      未配置 503 `auth_unconfigured` → 无 Bearer 401 → 不匹配 403 →
      命中放行（`label` = 命中 entry 的标签）。
    """
    if classify_route(path, method) in (KIND_HEALTH, KIND_READ):
        return _ALLOW
    token_set = tokens if tokens is not None else TokenSet()
    if not token_set.valid:
        return AuthDecision(
            allowed=False,
            status=503,
            code="auth_config_invalid",
            error=CONFIG_INVALID_ERROR,
        )
    if not token_set.entries:
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
    label = token_set.match(provided)
    if not label:
        # 不区分「接近命中哪个 label」：403 文案与单一 token 时代一致
        return AuthDecision(
            allowed=False,
            status=403,
            code="token_invalid",
            error=FORBIDDEN_ERROR,
        )
    return AuthDecision(allowed=True, status=200, code="ok", label=label)
