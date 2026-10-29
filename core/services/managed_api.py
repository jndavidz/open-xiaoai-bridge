"""托管播放 HTTP/SSE 适配层（B-05）。

路由（全部要求 music-control 专用凭据，target 与会话一致）：

    POST /api/music/sessions                       开一个托管会话
    GET  /api/music/sessions/{sid}                 快照
    POST /api/music/sessions/{sid}/commands        命令（幂等回执）
    GET  /api/music/sessions/{sid}/events          SSE（Last-Event-ID / 缺口 reset）

失败关闭：未配置凭据 ⇒ 503；缺凭据 401；未知凭据或 target 越权 403。
"""

from __future__ import annotations

import json

from aiohttp import web

from core.music.auth import MusicAuth, MusicAuthError
from core.music.model import ManagedError, SessionNotFound, StaleEpoch
from core.music.service import ConflictError, ManagedService, TargetMismatch


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


class ManagedAPI:
    def __init__(self, service: ManagedService, auth: MusicAuth) -> None:
        self.service = service
        self.auth = auth

    def register(self, app: web.Application) -> None:
        app.middlewares.append(_auth_middleware)
        app.router.add_post("/api/music/sessions", self.create_session)
        app.router.add_get("/api/music/sessions/{sid}", self.get_session)
        app.router.add_post("/api/music/sessions/{sid}/commands", self.post_command)
        app.router.add_get("/api/music/sessions/{sid}/events", self.stream_events)

    # ---- 鉴权 ----

    @staticmethod
    def _token(request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("X-Music-Token", "")

    def _authorize(self, request: web.Request, target: str):
        """失败关闭：MusicAuthError 由 `_auth_middleware` 统一转成 JSON 错误。"""

        return self.auth.authorize(self._token(request), target)

    # ---- 会话 ----

    async def create_session(self, request: web.Request) -> web.Response:
        body = await _body(request)
        target = str(body.get("target") or "")
        grant = self._authorize(request, target)
        snapshot = self.service.open_session(
            target=target,
            transport=str(body.get("transport") or "dlna"),
            player_backend=str(body.get("player_backend") or "mediaplayer"),
            lease_id=str(body.get("lease_id") or ""),
            principal=grant.principal,
        )
        return _json({"code": 200, "data": snapshot}, 201)

    async def get_session(self, request: web.Request) -> web.Response:
        target = str(request.query.get("target") or "")
        self._authorize(request, target)
        try:
            return _json({"code": 200, "data": self.service.snapshot(request.match_info["sid"])})
        except ManagedError:
            return _reject(404, "unknown session")

    # ---- 命令 ----

    async def post_command(self, request: web.Request) -> web.Response:
        body = await _body(request)
        target = str(body.get("target") or "")
        grant = self._authorize(request, target)
        try:
            receipt = self.service.command(
                request.match_info["sid"],
                kind=str(body.get("kind") or ""),
                payload=body.get("payload") or {},
                principal=grant.principal,
                idem_key=str(body.get("idem_key") or ""),
                expected_epoch=body.get("expected_epoch"),
                target=target or None,
            )
        except ConflictError as err:
            return _reject(409, str(err), code="idempotency_conflict")
        except TargetMismatch as err:
            return _reject(403, str(err), code="target_mismatch")
        except StaleEpoch as err:
            return _reject(409, str(err), code="stale_epoch")
        except SessionNotFound:
            return _reject(404, "unknown session")
        except ManagedError as err:
            return _reject(400, str(err))
        return _json({"code": 200, "data": receipt.as_dict()},
                     200 if receipt.replayed else 202)

    # ---- SSE ----

    async def stream_events(self, request: web.Request) -> web.StreamResponse:
        sid = request.match_info["sid"]
        target = str(request.query.get("target") or "")
        self._authorize(request, target)
        try:
            snapshot = self.service.snapshot(sid)
        except ManagedError:
            return _reject(404, "unknown session")

        try:
            after = int(request.headers.get("Last-Event-ID", "0") or 0)
        except ValueError:
            after = 0

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        subscription = self.service.bus.subscribe(sid)
        try:
            events, gap = self.service.bus.replay(sid, after)
            if gap:
                await _send_reset(response, snapshot, self.service.store.last_event_seq(sid))
            else:
                for event in events:
                    await _send_event(response, event)
            while True:
                event = await subscription.get(timeout=15.0)
                if event is None:
                    await response.write(b": ping\n\n")
                    continue
                if event.get("kind") == "reset":
                    await _send_reset(response, self.service.snapshot(sid),
                                      self.service.store.last_event_seq(sid))
                    continue
                await _send_event(response, event)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self.service.bus.unsubscribe(subscription)
        return response


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except MusicAuthError as err:
        return _reject(err.code, err.msg, code="music_scope")


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _reject(status: int, msg: str, *, code: str | None = None) -> web.Response:
    return _json({"code": status, "msg": msg, "error": code or msg}, status)


async def _send_event(response: web.StreamResponse, event: dict) -> None:
    payload = json.dumps(event, ensure_ascii=False)
    chunk = f"id: {event.get('seq', 0)}\nevent: {event.get('kind', 'event')}\ndata: {payload}\n\n"
    await response.write(chunk.encode("utf-8"))


async def _send_reset(response: web.StreamResponse, snapshot: dict, seq: int) -> None:
    payload = json.dumps({"kind": "reset", "seq": seq, "snapshot": snapshot}, ensure_ascii=False)
    await response.write(f"id: {seq}\nevent: reset\ndata: {payload}\n\n".encode("utf-8"))
