"""托管播放 HTTP/SSE 适配层（B-05）。

路由（全部要求 music-control 专用凭据，target 与会话一致）：

    POST /api/music/sessions                       开一个托管会话
    GET  /api/music/sessions/{sid}                 快照
    POST /api/music/sessions/{sid}/commands        命令（幂等回执）
    GET  /api/music/sessions/{sid}/events          SSE（Last-Event-ID / 缺口 reset）

失败关闭：未配置凭据 ⇒ 503；缺凭据 401；未知凭据或 target 越权 403。
"""

from __future__ import annotations

import asyncio
import json
import os

from aiohttp import web

from core.music.auth import MusicAuth, MusicAuthError
from core.music.budget import BudgetExhausted, RetryBudget
from core.music.model import ManagedError, SessionNotFound, StaleEpoch, TrackRef
from core.music.service import ConflictError, ManagedService, TargetMismatch

DEFAULT_BUDGET_MS = 15000
DEVICE_COMMANDS = ("play", "pause", "resume", "next", "prev", "stop")


def _command_budget() -> RetryBudget:
    """每次命令一条端到端预算（最外层持有；下游只能消耗剩余）。"""

    try:
        total = float(os.environ.get("MANAGED_COMMAND_BUDGET_MS", DEFAULT_BUDGET_MS))
    except (TypeError, ValueError):
        total = DEFAULT_BUDGET_MS
    return RetryBudget(total)


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


class ManagedAPI:
    def __init__(self, service: ManagedService, auth: MusicAuth,
                 *, cors_origins: tuple[str, ...] = ()) -> None:
        self.service = service
        self.auth = auth
        self.cors_origins = tuple(o for o in cors_origins if o)
        self._device_executor = None
        self._device_tasks: set = set()

    def set_device_executor(self, fn) -> None:
        """设备执行侧（B-08）：命令先落回执，设备动作异步执行并回收 observed/failed。

        接受命令不等待下载/转码/设备 RPC——停播与起播都在后台任务里完成。
        """

        self._device_executor = fn

    def register(self, app: web.Application) -> None:
        # 顺序：CORS 在外、鉴权在内——预检不要求业务凭据，实际请求仍失败关闭。
        app.middlewares.append(_cors_middleware(self.cors_origins))
        app.middlewares.append(_auth_middleware)
        app.router.add_post("/api/music/sessions", self.create_session)
        app.router.add_get("/api/music/sessions/{sid}", self.get_session)
        app.router.add_post("/api/music/sessions/{sid}/queue", self.post_queue)
        app.router.add_post("/api/music/sessions/{sid}/commands", self.post_command)
        app.router.add_get("/api/music/sessions/{sid}/events", self.stream_events)
        for path in ("/api/music/sessions", "/api/music/sessions/{sid}",
                     "/api/music/sessions/{sid}/queue",
                     "/api/music/sessions/{sid}/commands",
                     "/api/music/sessions/{sid}/events"):
            app.router.add_route("OPTIONS", path, self.preflight)

    async def preflight(self, request: web.Request) -> web.Response:
        """CORS 预检：仅回 204，是否带上 ACAO 由 CORS 中间件的白名单决定。"""

        return web.Response(status=204)

    # ---- 鉴权 ----

    @staticmethod
    def _token(request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("X-Music-Token", "")

    def _authorize(self, request: web.Request, target: str, scope: str):
        """失败关闭：凭据必须同时满足 target 白名单与端点 scope。"""

        grant = self.auth.authorize(self._token(request), target)
        if not grant.can(scope):
            raise MusicAuthError(403, f"credential lacks scope {scope!r}")
        return grant

    @staticmethod
    def _check_session_target(grant, snapshot: dict, target: str) -> None:
        session_target = str(snapshot.get("target") or "")
        if session_target and session_target != target:
            raise MusicAuthError(403, "target does not match the session")

    # ---- 会话 ----

    async def create_session(self, request: web.Request) -> web.Response:
        body = await _body(request)
        target = str(body.get("target") or "")
        grant = self._authorize(request, target, "command")
        snapshot = self.service.open_session(
            target=target,
            transport=str(body.get("transport") or "dlna"),
            player_backend=str(body.get("player_backend") or "mediaplayer"),
            lease_id=str(body.get("lease_id") or ""),
            principal=grant.principal,
        )
        return _json({"code": 200, "data": snapshot}, 201)

    async def post_queue(self, request: web.Request) -> web.Response:
        """入队一条 TrackRef（B-08/S-06：手机与桥接域都只发身份，不发 URL）。"""
        body = await _body(request)
        target = str(body.get("target") or "")
        grant = self._authorize(request, target, "command")
        ref = (body.get("ref") or {})
        try:
            track_ref = TrackRef.make(
                str(ref.get("source") or ""),
                str(ref.get("id") or ""),
                ref.get("extra") or {},
            )
            entry = self.service.queue_add(
                request.match_info["sid"],
                track_ref,
                quality=str(body.get("quality") or "320k"),
                principal=grant.principal,
                expected_epoch=body.get("expected_epoch"),
            )
        except StaleEpoch as err:
            return _reject(409, str(err), code="stale_epoch")
        except ManagedError as err:
            return _reject(400, str(err))
        return _json({"code": 200, "data": {"entry_id": entry.entry_id,
                                            "quality": entry.quality}}, 200)

    async def get_session(self, request: web.Request) -> web.Response:
        target = str(request.query.get("target") or "")
        grant = self._authorize(request, target, "read")
        try:
            snapshot = self.service.snapshot(request.match_info["sid"])
            self._check_session_target(grant, snapshot, target)
            return _json({"code": 200, "data": snapshot})
        except ManagedError:
            return _reject(404, "unknown session")

    # ---- 命令 ----

    async def post_command(self, request: web.Request) -> web.Response:
        body = await _body(request)
        target = str(body.get("target") or "")
        grant = self._authorize(request, target, "command")
        try:
            receipt = self.service.command(
                request.match_info["sid"],
                kind=str(body.get("kind") or ""),
                payload=body.get("payload") or {},
                principal=grant.principal,
                idem_key=str(body.get("idem_key") or ""),
                expected_epoch=body.get("expected_epoch"),
                target=target or None,
                budget=_command_budget(),
            )
        except BudgetExhausted as err:
            return _reject(504, str(err), code="budget_exhausted")
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
        if not receipt.replayed:
            self._schedule_device(request.match_info["sid"], str(body.get("kind") or "").strip().lower(),
                                  receipt.receipt_id)
        return _json({"code": 200, "data": receipt.as_dict()},
                     200 if receipt.replayed else 202)

    def _schedule_device(self, session_id: str, kind: str, receipt_id: str) -> None:
        if self._device_executor is None or kind not in DEVICE_COMMANDS:
            return
        task = asyncio.ensure_future(self._run_device(session_id, kind, receipt_id))
        self._device_tasks.add(task)
        task.add_done_callback(self._device_tasks.discard)

    async def _run_device(self, session_id: str, kind: str, receipt_id: str) -> None:
        from core.utils.logger import logger

        try:
            await self._device_executor(session_id, kind, receipt_id)
        except Exception as exc:  # noqa: BLE001 - 设备执行异常不得影响 HTTP 层
            logger.warning(f"[ManagedAPI] device {kind} failed: {type(exc).__name__}: {exc}")

    # ---- SSE ----

    async def stream_events(self, request: web.Request) -> web.StreamResponse:
        sid = request.match_info["sid"]
        target = str(request.query.get("target") or "")
        grant = self._authorize(request, target, "stream")
        try:
            snapshot = self.service.snapshot(sid)
            self._check_session_target(grant, snapshot, target)
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
            sent_seq = after
            if gap:
                sent_seq = self.service.store.last_event_seq(sid)
                await _send_reset(response, snapshot, sent_seq)
            else:
                for event in events:
                    await _send_event(response, event)
                    sent_seq = max(sent_seq, int(event.get("seq") or 0))
            while True:
                event = await subscription.get(timeout=15.0)
                if event is None:
                    await response.write(b": ping\n\n")
                    continue
                if event.get("kind") == "reset":
                    sent_seq = self.service.store.last_event_seq(sid)
                    await _send_reset(response, self.service.snapshot(sid), sent_seq)
                    continue
                seq = int(event.get("seq") or 0)
                if seq and seq <= sent_seq:
                    continue   # 先订阅后回放：重复帧在这里丢掉
                await _send_event(response, event)
                sent_seq = max(sent_seq, seq)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self.service.bus.unsubscribe(subscription)
        return response


def _cors_middleware(allowed: tuple[str, ...]):
    """托管面的 CORS：默认**不放开**（浏览器跨源被拒）；只有白名单 origin 放行。"""

    @web.middleware
    async def middleware(request: web.Request, handler):
        try:
            response = await handler(request)
        except web.HTTPMethodNotAllowed as err:
            response = err
        origin = request.headers.get("Origin", "")
        if origin and origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type, X-Music-Token, Last-Event-ID")
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    return middleware


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
