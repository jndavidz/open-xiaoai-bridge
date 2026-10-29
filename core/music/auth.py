"""music-control 专用鉴权（B-05）。

与业务 API 的 Bearer token 完全分离：**既有 bridge token 不会自动获得托管控制权**。
每个 token 绑定 principal 与允许的 target 列表，端点与 target 同时受限。

配置（JSON，环境变量注入）：
    {"<token>": {"name": "songloft", "targets": ["lx06"]}}

失败关闭：
    - 未配置 ⇒ 503（`auth_config_missing`）；
    - 配置非法 ⇒ 503（`auth_config_invalid`）；
    - 缺 token ⇒ 401；token 未知 ⇒ 403；target 不在白名单 / 未声明 target ⇒ 403。
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass


class MusicAuthError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = int(code), str(msg)


SCOPES: frozenset[str] = frozenset({"read", "command", "stream"})


@dataclass(frozen=True)
class MusicGrant:
    principal: str
    targets: frozenset[str]
    scopes: frozenset[str] = SCOPES

    def allows(self, target: str) -> bool:
        return bool(target) and target in self.targets

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class MusicAuth:
    def __init__(self, raw: str = "") -> None:
        self._grants: list[tuple[str, MusicGrant]] = []
        raw = (raw or "").strip()
        self.configured = bool(raw)
        self.invalid = False
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except ValueError:
            self.invalid = True
            return
        if not isinstance(parsed, dict) or not parsed:
            self.invalid = True
            return
        for token, entry in parsed.items():
            targets = entry.get("targets") if isinstance(entry, dict) else entry
            name = entry.get("name") if isinstance(entry, dict) else ""
            if not token or not isinstance(targets, list) or not targets:
                self.invalid = True
                return
            cleaned = {str(t).strip() for t in targets if str(t).strip()}
            if not cleaned:
                self.invalid = True
                return
            raw_scopes = entry.get("scopes") if isinstance(entry, dict) else None
            if raw_scopes is None:
                scopes = SCOPES
            elif isinstance(raw_scopes, list) and raw_scopes:
                scopes = frozenset(str(s) for s in raw_scopes)
                if not scopes <= SCOPES:
                    self.invalid = True
                    return
            else:
                self.invalid = True
                return
            self._grants.append(
                (str(token), MusicGrant(principal=str(name or "music"),
                                        targets=frozenset(cleaned), scopes=scopes))
            )

    def authorize(self, token: str, target: str) -> MusicGrant:
        if not self.configured:
            raise MusicAuthError(503, "music-control auth is not configured")
        if self.invalid:
            raise MusicAuthError(503, "music-control auth config is invalid")
        token = str(token or "")
        if not token:
            raise MusicAuthError(401, "missing music-control credential")
        for known, grant in self._grants:
            if _same(token, known):
                if not grant.allows(str(target or "")):
                    raise MusicAuthError(403, "target is not allowed for this credential")
                return grant
        raise MusicAuthError(403, "unknown music-control credential")
