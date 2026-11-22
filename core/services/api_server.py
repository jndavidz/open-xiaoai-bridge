"""
HTTP API Server for XiaoZhi
Provides endpoints to play text/audio remotely
"""

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import Coroutine
from typing import Any

import open_xiaoai_server
from aiohttp import web
from core.device import get_playback_events
from core.ref import get_speaker, get_xiaoai, get_kws
from core.wakeup_session import EventManager
from core.services.admin_api import AdminAPI
from core.services.auth import authorize, classify_route, configured_tokens
from core.services.caller import resolve_caller_tag, trust_proxy_enabled
from core.services.health import build_health_payload
from core.services.speaker import AnnounceRejected
from core.services.inject_control import (
    INJECT_DISABLED_CODE,
    INJECT_DISABLED_ERROR,
    InjectDataAuth,
    open_injection_window,
    peer_allowed,
    resolve_inject_data_auth,
    resolve_inject_enabled,
)
from core.services.external_text import (
    EXTERNAL_TEXT_START_FAILED_CODE,
    EXTERNAL_TEXT_BACKEND_UNKNOWN_CODE,
    EXTERNAL_TEXT_BUSY_CODE,
    EXTERNAL_TEXT_DISABLED_CODE,
    EXTERNAL_TEXT_RATE_LIMITED_CODE,
    EXTERNAL_TEXT_REFUSED_CODE,
    ExternalTextThrottle,
    resolve_external_text_backend,
    resolve_external_text_cidrs,
    resolve_external_text_enabled,
    validate_external_text_payload,
)
from core.services.tts.doubao import DoubaoTTS
from core.utils.config import ConfigManager
from core.utils.logger import logger


# 注入中继单例引用（wakeup_session 的 TTL 失效钩子经 get_relay() 同步关窗；
# APIServer.__init__ 赋值。避免模块级循环导入，延迟解析。）
_relay_singleton = None


def get_relay():
    return _relay_singleton


class InjectRelay:
    """注入流式中继（数据面 rendezvous）。

    生产形态（firmware-asr-injection-feasibility.md §6.1/§7）：
      - PC 经 `POST /api/inject/stream`（chunked）上行 PCM → 本中继缓冲
      - 音箱 hook 作 TCP 客户端连 `:9093`（INJECT_TCP=bridge:9093）拉流
      - **闸门强绑定**：仅注入窗口开启期间转发；窗口关/超时 → 断开下游并丢弃上行
        （PC 侧时序 bug 最多导致流被丢，不可能绕过闸门防护）

    不落盘、纯内存 chunk 转发；LAN 双跳转发延迟 <2ms（相对云端 ASR 数百 ms 可忽略）。

    ## 文本回传：单调序号 + 有界回放（2026-10-21）

    文本回传是 **pull-ish 语义**：WS 订阅者晚于 ASR 结果连上是常态
    （推流→出结果往往同一瞬间完成，而 WS 建连/订阅有调度延迟）。

    因此不能用「窗口缓冲 + 清空」：清空时机与它要修的晚连是同类问题——
    PC 在窗口结束后才连 WS（正常操作：松开快捷键后工具才连），
    此时缓冲已被 `open_session` 清掉。

    改用：
      - `_seq` 单调递增（永不复位）→ 客户端可去重/排序/断点续取
      - `_text_log` 有界裁剪（只保留最近 MAX_TEXT_LOG 条）→ 内存有上界
      - 实时通道同样带 seq → 重放与实时靠 seq 对齐，不依赖时序运气
    """

    # 文本回放缓冲上限（条）。一条 ASR 文本通常 ≤ 数百字节，200 条 ≈ 数十 KB。
    MAX_TEXT_LOG = 200

    def __init__(self, enabled: bool = True):
        # 模块总开关（ticket 07）：关闭时本对象仍存在（便于健康接口报告
        # enabled=false），但**不启动下游 :9093**，所有数据面端点 fail closed。
        self.enabled: bool = bool(enabled)
        self.chunks: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.active: bool = False
        self.downstream_writer: asyncio.StreamWriter | None = None
        # 数据面鉴权（ticket 12）：start() 时判定；None = 尚未判定/未启动
        self.data_auth: InjectDataAuth | None = None
        self._downstream_task: asyncio.Task | None = None
        # 存活的连接处理器（client_connected 任务）。py3.12 的 Server.wait_closed()
        # 会等全部 handler 退出，而 handler 会一直挂在 chunks.get() 上
        # ⇒ stop() 必须显式取消它们，否则永不返回。
        self._conn_tasks: set[asyncio.Task] = set()
        self.text_queues: list[asyncio.Queue] = []   # 文本回传订阅者（WS，多客户端广播）
        self._text_log: list[tuple[int, str]] = []   # 有界文本日志 [(seq, text)]，不随窗口清
        self._seq: int = 0                            # 文本序号，单调递增，永不复位
        self._window_start_seq: int = 0               # 最近一次 open_session 时的 seq
        self.stats = {"up_bytes": 0, "down_bytes": 0, "sessions": 0, "texts": 0}

    @property
    def auth_mode(self) -> str:
        """数据面鉴权模式（cidr/anon/refused）；未判定时 unknown（观测用）。"""
        return self.data_auth.mode if self.data_auth is not None else "unknown"

    @staticmethod
    def _disabled_response() -> web.Response:
        """统一「注入模块已关闭」响应（503 + code=inject_disabled）。

        关闭语义（与 core/services/inject_control.py 的模块文档一致）：
          - 控制面/数据面端点一律 503，不用 404/200 混淆「模块没开」与「路径不存在」；
          - 唯一例外是 `GET /api/inject` 只读探针（200 + enabled=false），
            由 APIServer.handle_inject_status 处理，便于区分「模块关闭」与「窗口未开」。
        """
        return web.json_response(
            {
                "success": False,
                "enabled": False,
                "code": INJECT_DISABLED_CODE,
                "error": INJECT_DISABLED_ERROR,
            },
            status=503,
        )

    def open_session(self) -> None:
        """开窗：允许上行数据入队。

        ⚠️ 不清理 `_text_log`——文本回放跨窗口保留（见类文档）。
        窗口语义只约束「上行音频是否转发」与「实时闸门」，不约束文本历史。
        """
        self.active = True
        self._window_start_seq = self._seq
        self.stats["sessions"] += 1
        self._notify_subscribers("window_open")
        logger.info("[InjectRelay] 会话开启（上游可推流）")

    def close_session(self, reason: str = "closed") -> None:
        """关窗：丢弃后续上行 + 断开下游 hook（hook 会自动重连等下一窗）。"""
        if not self.active and self.downstream_writer is None:
            return
        self.active = False
        self._notify_subscribers("window_closed")
        # 断开下游：hook 收到 EOF 后按退避重连，窗口未开时连上也只会被立即断开
        w = self.downstream_writer
        if w is not None:
            self.downstream_writer = None
            try:
                w.close()
            except Exception:
                pass
        logger.info(f"[InjectRelay] 会话关闭 ({reason})，累计 up={self.stats['up_bytes']} down={self.stats['down_bytes']}")

    async def handle_upstream(self, request: web.Request) -> web.Response:
        """POST /api/inject/stream —— PC 上行 PCM（chunked body）。

        窗口未开时直接 409 拒绝（客户端应先 POST /api/inject 开窗）。
        模块关闭时 503（fail closed，不分配缓冲、不推进任何统计）。
        """
        if not self.enabled:
            return self._disabled_response()
        if not self.active:
            return web.json_response(
                {"success": False, "error": "injection window not open"},
                status=409,
            )
        peer = request.transport.get_extra_info("peername") if request.transport else None
        logger.info(f"[InjectRelay] 上行开始 peer={peer}")
        dropped = 0
        try:
            async for chunk in request.content.iter_any():
                if not self.active:   # 窗口中途关闭 → 停止接收
                    break
                self.stats["up_bytes"] += len(chunk)
                try:
                    self.chunks.put_nowait(chunk)
                except asyncio.QueueFull:
                    dropped += len(chunk)   # 下游阻塞 → 丢弃最旧策略：直接丢新块（保实时性）
            return web.json_response({
                "success": True,
                "up_bytes": self.stats["up_bytes"],
                "dropped_bytes": dropped,
            })
        except Exception as e:
            logger.warning(f"[InjectRelay] 上行异常: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)
        finally:
            logger.info(f"[InjectRelay] 上行结束 up={self.stats['up_bytes']} dropped={dropped}")

    async def downstream_server(self, host: str, port: int) -> None:
        """hook 下行 TCP 服务端（:9093）：单客户端，鉴权通过后持续推送缓冲中的 PCM。

        连接处理（ticket 12，先认证后推流）：CIDR 判定通过**才**接管
        `downstream_writer`——被拒连接不写入、不推送、直接断开；
        已有认证连接被新连接顶替时告警（正常只有一台设备，替换=可疑）。
        """
        async def client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            conn_task = asyncio.current_task()
            if conn_task is not None:
                self._conn_tasks.add(conn_task)
            peer = writer.get_extra_info("peername")
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else str(peer or "")
            auth = self.data_auth
            if auth is not None and auth.mode == "cidr" and not peer_allowed(
                peer_ip, auth.networks
            ):
                logger.warning(
                    f"[InjectRelay] 下游连接拒绝 auth=reject(reason=cidr) peer={peer_ip}"
                )
                try:
                    writer.close()
                except Exception:
                    pass
                if conn_task is not None:
                    self._conn_tasks.discard(conn_task)
                return
            replaced = self.downstream_writer is not None
            if replaced:
                logger.warning(
                    f"[InjectRelay] 下游连接替换 auth=ok replaced=true peer={peer_ip}"
                    "（正常仅一台设备，替换需排查同段冒充）"
                )
            else:
                logger.info(
                    f"[InjectRelay] 下游连接 auth=ok replaced=false peer={peer_ip}"
                )
            self.downstream_writer = writer
            try:
                while True:
                    chunk = await self.chunks.get()
                    if chunk is None:   # 会话结束哨兵
                        break
                    if not self.active and not self.chunks.qsize():
                        break
                    writer.write(chunk)
                    self.stats["down_bytes"] += len(chunk)
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            finally:
                # 只在自己仍是接管者时清空：被替换后旧处理器退出不得误清新连接
                if self.downstream_writer is writer:
                    self.downstream_writer = None
                try:
                    writer.close()
                except Exception:
                    pass
                if conn_task is not None:
                    self._conn_tasks.discard(conn_task)
                logger.info("[InjectRelay] 下游连接结束")

        server = await asyncio.start_server(client_connected, host, port)
        logger.info(f"[InjectRelay] 下游 TCP 服务端就绪 :{port}（hook 客户端拉流）")
        async with server:
            await server.serve_forever()

    async def start(self, host: str, port: int) -> None:
        if not self.enabled:
            # ticket 07：模块关闭时不监听 :9093，不占用端口（hook 连不上即自然退避）
            logger.info("[InjectRelay] 注入模块未启用，跳过下游 TCP 服务启动")
            return
        # ticket 12：启用 ≠ 可以裸奔。无 CIDR、无（未来实现的）共享密钥、
        # 又未显式 INJECT_ALLOW_ANON=1 时拒绝监听，控制面端点不受影响。
        self.data_auth = resolve_inject_data_auth()
        if self.data_auth.mode == "refused":
            logger.error(
                f"[InjectRelay] 拒绝启动 :{port} 数据面"
                f"（inject data plane: auth=refused）：{self.data_auth.reason}"
            )
            return
        allow = ""
        if self.data_auth.mode == "cidr":
            allow = " allow=" + ",".join(str(n) for n in self.data_auth.networks)
        logger.info(f"[InjectRelay] inject data plane: auth={self.data_auth.mode}{allow}")
        self._downstream_task = asyncio.create_task(self.downstream_server(host, port))

    async def stop(self) -> None:
        self.close_session("relay stop")
        # 先取消连接处理器再取消 server 任务：wait_closed 要等 handler 全部退出
        for task in list(self._conn_tasks):
            task.cancel()
        if self._downstream_task:
            self._downstream_task.cancel()
        pending = [
            t for t in ([self._downstream_task] if self._downstream_task else [])
            + list(self._conn_tasks)
            if t is not None and not t.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ---- 文本回传（bridge → PC，WS 广播）----

    def _notify_subscribers(self, event: str) -> None:
        """窗口状态变化 → 各订阅者队列插一条通知。"""
        for q in list(self.text_queues):
            try:
                q.put_nowait(f"__event__:{event}")
            except asyncio.QueueFull:
                pass

    def publish_text(self, text: str, is_final: bool = False) -> None:
        """闸门拦截点调用：把识别文本广播给所有 WS 订阅者（仅窗口开启期）。

        同时写入 `_text_log`（带单调 seq）——WS 订阅者连接晚于 ASR 结果时
        （推流瞬间完成），连上后按 seq 回放，保证不丢文本且可去重。

        `is_final` 语义（2026-10-21 真机实测）：云端 ASR 对**一句话**会发**多条**
        `RecognizeResult`，且是「累积快照」不是增量。实测「今天天气怎么样」的一句：

            第1–2 条 text=""            （is_final=false）
            第3 条   text="今"          （is_vad_begin=true）
            第4–6 条 text="今天天气"/"今天天气怎么"/"今天天气怎么样"
            第7 条   text="今天天气怎么样"（is_nlp_request=true, is_stop=true）
            第8 条   text="今天天气怎么样"（**is_final=true**）

        ⇒ **必须把 `is_final` 透传给客户端**：否则 PC 端无法区分「还在说」与「说完了」，
        且第 7/8 条文本相同（不区分就会重复插入）。
        中间快照**不过滤**：语音输入法需要边说话边出字。
        """
        if not self.active or not text:
            return
        self.stats["texts"] += 1
        self._seq += 1
        seq = self._seq
        self._text_log.append((seq, text, bool(is_final)))
        if len(self._text_log) > self.MAX_TEXT_LOG:
            # 有界裁剪：只保留最近 MAX_TEXT_LOG 条，保证内存上界
            del self._text_log[: len(self._text_log) - self.MAX_TEXT_LOG]
        for q in list(self.text_queues):
            try:
                q.put_nowait((seq, text, bool(is_final)))
            except asyncio.QueueFull:
                logger.warning(
                    "[InjectRelay] 文本订阅队列满，丢弃实时推送"
                    "（客户端可经 ?since= 重连补拉）"
                )

    async def handle_ws_text(self, request: web.Request) -> web.WebSocketResponse:
        """GET /api/inject/text?since=N (WebSocket) —— PC 订阅识别文本。

        `since` 语义与回放/实时顺序保证见 `_serve_text_subscription`。
        模块关闭时在 WS 握手前返回 503（不升级连接、不回放任何文本）。
        """
        if not self.enabled:
            return self._disabled_response()
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        raw_since = request.query.get("since")
        # 缺省 since 只回放「最近一次开窗之后」的文本；显式 since=0 才回放全部保留历史。
        # 避免新窗口/新客户端把上一轮语音文本重新插入光标。
        since = self.resolve_since(raw_since, self._window_start_seq)
        await self._serve_text_subscription(ws, since)
        return ws

    @staticmethod
    def resolve_since(raw, window_start: int) -> int:
        """缺省 since 用窗口起点；显式值走 `parse_since`（0 = 全部历史）。"""

        if raw is None or raw == "":
            return max(0, int(window_start))
        return InjectRelay.parse_since(raw)

    @staticmethod
    def parse_since(raw) -> int:
        """解析 `?since=`；非法 → 0（显式 0 = 回放全部保留历史）。"""
        try:
            value = int(raw or 0)
        except (TypeError, ValueError):
            return 0
        return max(0, value)

    async def _serve_text_subscription(self, ws, since: int) -> None:
        """在已准备好的 WS 上服务文本订阅（与传输层解耦，便于测试）。

        回放语义（为什么不是「窗口缓冲」）：
          - `since` 0        → 回放当前保留的全部历史（最多 MAX_TEXT_LOG 条）
          - `since` N (>0)   → 只回放 seq > N 的文本（断点续取，客户端记最后 seq）
        每条文本都带 `seq`，客户端可据此去重与排序。

        ⚠️ 顺序保证：**先回放，再订阅实时队列**。若先 `create_task` 再回放，
        `await ws.send_json` 会让出控制权 ⇒ 实时推送可能插到回放中途 ⇒ 乱序。
        （即使如此，客户端也能靠 seq 纠正，但服务端应保证不乱序。）
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        push_task: asyncio.Task | None = None
        try:
            await self._notify_state(ws)
            # ① 先回放历史（此时尚未订阅实时队列，不可能有实时插队）
            replayed = 0
            for seq, t, is_final in list(self._text_log):
                if seq > since:
                    await ws.send_json(
                        {"event": "text", "seq": seq, "text": t, "is_final": is_final}
                    )
                    replayed += 1
            # ② 回放完再订阅实时队列（消除顺序陷阱）
            self.text_queues.append(q)
            push_task = asyncio.create_task(self._push_texts(ws, q))
            logger.info(
                f"[InjectRelay] 文本订阅接入（当前 {len(self.text_queues)} 个，"
                f"since={since} 回放 {replayed} 条）"
            )

            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    if msg.data == "ping":
                        await ws.send_json({"event": "pong"})
                    continue
                if msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                    break
        finally:
            if push_task is not None:
                push_task.cancel()
            if q in self.text_queues:
                self.text_queues.remove(q)
            logger.info(f"[InjectRelay] 文本订阅断开（剩 {len(self.text_queues)} 个）")

    async def _notify_state(self, ws: web.WebSocketResponse):
        await ws.send_json({
            "event": "window_open" if self.active else "window_closed"
        })

    async def _push_texts(self, ws: web.WebSocketResponse, q: asyncio.Queue):
        while True:
            item = await q.get()
            if isinstance(item, str) and item.startswith("__event__:"):
                await ws.send_json({"event": item.split(":", 1)[1]})
            else:
                seq, text, is_final = item
                await ws.send_json(
                    {"event": "text", "seq": seq, "text": text, "is_final": is_final}
                )

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /api/inject/relay —— 中继观测（模块关闭时 503，不泄露任何缓冲内容）。"""
        if not self.enabled:
            return self._disabled_response()
        return web.json_response({
            "success": True,
            "enabled": True,
            "active": self.active,
            "stats": self.stats,
            "queue": self.chunks.qsize(),
        })


class APIServer:
    """HTTP API Server to control XiaoZhi speaker remotely"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self.config = ConfigManager.instance()
        # Admin 面板鉴权中间件（只拦 /api/admin/*）
        self.admin_api = AdminAPI()
        # 业务 API 鉴权中间件（ticket 06）：与 ADMIN_TOKEN 分离，用 BRIDGE_API_TOKEN。
        # 顺序：业务鉴权在最外层（/api/admin/* 由它直接放行给 Admin 中间件）。
        self.app = web.Application(
            middlewares=[self.traceparent_middleware, self.api_auth_middleware,
                         self.admin_api.auth_middleware]
        )
        self.runner = None
        self.site = None
        # 注入模块总开关（ticket 07）：默认关闭；env INJECT_ENABLE 可覆盖 config。
        # 关闭时中继不监听 :9093，控制/数据面端点 fail closed（见 inject_control 文档）。
        self.inject_enabled = resolve_inject_enabled(self.config)
        self.inject_relay = InjectRelay(enabled=self.inject_enabled)   # 注入流式中继（数据面）
        self._inject_expire_task: asyncio.Task | None = None
        # 票 23：历史点击 worker 任务（cch_click_enabled=False 时不创建）
        self._cch_click_task: asyncio.Task | None = None
        # 外部文本轮 per-caller 限流器（B-37）：键 = token label + TCP peer
        self._external_text_throttle = ExternalTextThrottle()
        global _relay_singleton
        _relay_singleton = self.inject_relay
        self._setup_routes()
        self.admin_api.register(self.app)

    _TRACEPARENT_RE = re.compile(
        r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
    )

    @web.middleware
    async def traceparent_middleware(self, request: web.Request, handler):
        """接受 W3C `traceparent`：回显同一头并把 trace id 记进调试日志。

        只做链路关联，不伪造/改写调用方的 trace；格式非法一律忽略。
        """
        tp = request.headers.get("traceparent", "")
        response = await handler(request)
        if tp and self._TRACEPARENT_RE.match(tp):
            response.headers["traceparent"] = tp
            logger.debug(f"[APIServer] traceparent trace_id={tp.split('-')[1]} {request.method} {request.path}")
        return response

    @web.middleware
    async def api_auth_middleware(self, request: web.Request, handler):
        """业务 API 鉴权：写类端点要求 `Authorization: Bearer <BRIDGE_API_TOKEN>`。

        判定逻辑全在 `core/services/auth.authorize`（纯函数，可单测）：
          - health/read（/api/health、/api/status 等）免鉴权
          - 写类端点：token 缺失/格式错 401、不匹配 403
          - 服务端未配置 BRIDGE_API_TOKEN → 写类端点 503（失败关闭）+ 配置提示
          - 管理面板（/api/admin/*、/admin）不在此判定，由 AdminAPI 的
            ADMIN_TOKEN 中间件负责（两套 token 分离）
        日志只记调用方与错误码，不打印 token 明文。
        """
        path = request.path
        if not path.startswith("/api/"):
            return await handler(request)
        if path == "/api/admin" or path.startswith("/api/admin/"):
            return await handler(request)
        # 托管播放面（B-05）有自己的 music-control 凭据（MANAGED_TOKENS），
        # 与业务 token 完全分离：不能先被 BRIDGE_API_TOKENS 拦掉。
        if path == "/api/music" or path.startswith("/api/music/"):
            return await handler(request)
        # 每个请求重新读 env（与单一 token 时代口径一致：零成本、无缓存失效问题）
        decision = authorize(
            path,
            request.method,
            authorization=request.headers.get("Authorization"),
            tokens=configured_tokens(),
        )
        if decision.allowed:
            # 审计（ticket 12）：写类放行记 label（token 决定，不可伪造）+ peer。
            # 只读端点不记（高频轮询会刷屏）；health/read 本就免鉴权。
            if classify_route(path, request.method) == "write":
                # label 暂存进 request：后续 handler（如 /api/external/text 的
                # per-caller 限流键）取用——token 决定，不可伪造。
                request["auth_label"] = decision.label
                logger.info(
                    f"[APIServer] 放行 {request.method} {path} "
                    f"label={decision.label} caller={self._caller_tag(request)}"
                )
            return await handler(request)
        # 配置非法是服务端自身的错误（非客户端问题）：ERROR 级别促使尽快修复
        log = logger.error if decision.code == "auth_config_invalid" else logger.warning
        log(
            f"[APIServer] 拒绝 {request.method} {path} "
            f"caller={self._caller_tag(request)} code={decision.code} "
            f"label={decision.label or 'none'}"
        )
        return web.json_response(
            {"success": False, "error": decision.error, "code": decision.code},
            status=decision.status,
            headers=decision.header_dict(),
        )

    @staticmethod
    def _mic_gate_seconds(timeout_ms, extra: float = 5.0) -> float:
        """固件闭麦闸门的持有上限：以调用方 timeout 为界，留 margin，并钳位。

        ticket 10：超上限只是「告警 + 强制恢复快照」的兜底，不是播放时限。
        """
        try:
            seconds = float(timeout_ms) / 1000.0
        except (TypeError, ValueError):
            seconds = 600.0
        return max(30.0, min(seconds + extra, 900.0))

    def _setup_routes(self):
        """Setup API routes"""
        self.app.router.add_post("/api/play/text", self.handle_play_text)
        self.app.router.add_post("/api/play/url", self.handle_play_url)
        self.app.router.add_post("/api/play/file", self.handle_play_file)
        self.app.router.add_get("/api/status", self.handle_get_status)
        self.app.router.add_post("/api/wakeup", self.handle_wakeup)
        self.app.router.add_post("/api/interrupt", self.handle_stop)
        self.app.router.add_post("/api/audio_input", self.handle_audio_input)  # T7.6 恢复通道
        self.app.router.add_post("/api/inject", self.handle_inject)  # ASR 注入窗口（闸门A + P2唤醒）
        self.app.router.add_get("/api/inject", self.handle_inject_status)
        self.app.router.add_post("/api/external/text", self.handle_external_text)  # 外部 ASR 文本入口（B-37）
        self.app.router.add_post("/api/external/np", self.handle_external_np)  # CCH 音乐事件（票 03 编排面）
        self.app.router.add_post("/api/cch/collect", self.handle_cch_collect)  # CCH 收藏幂等（票 13）
        self.app.router.add_get("/api/cch/bg/{name}", self.handle_cch_bg)  # 虚化背景图（只读、键不可猜）
        self.app.router.add_post("/api/inject/stream", self.inject_relay.handle_upstream)  # PC 上行 PCM
        self.app.router.add_get("/api/inject/relay", self.inject_relay.handle_health)  # 中继观测
        self.app.router.add_get("/api/inject/text", self.inject_relay.handle_ws_text)  # 文本回传 WS
        self.app.router.add_get("/api/health", self.handle_health)
        self.app.router.add_get("/livez", self.handle_livez)
        self.app.router.add_get("/readyz", self.handle_readyz)
        # TTS endpoints
        self.app.router.add_post("/api/tts/doubao", self.handle_tts_doubao)
        self.app.router.add_get("/api/tts/doubao_voices", self.handle_tts_voices)
        self._setup_managed_routes()

    def _setup_managed_routes(self) -> None:
        """托管播放 API/SSE（B-05）：始终注册、失败关闭。

        未配置 `MANAGED_TOKENS` 时所有 /api/music/* 返回 503，既有端点不受影响；
        初始化失败（例如磁盘不可写）只告警，不阻断语音主链路。
        """

        try:
            import os

            from core.music import song_bridge
            from core.music.auth import MusicAuth
            from core.services.managed_api import ManagedAPI

            cors = tuple(
                origin.strip()
                for origin in os.environ.get("MANAGED_CORS_ORIGINS", "").split(",")
                if origin.strip()
            )
            # 与语音路径共用同一 runtime/store：HTTP 命令与「继续播放」等语音命令
            # 必须作用在同一条托管队列上，不能各持一份状态。
            runtime = song_bridge.build_runtime()
            self.managed_api = ManagedAPI(
                runtime.service,
                MusicAuth(os.environ.get("MANAGED_TOKENS", "")),
                cors_origins=cors,
            )
            self.managed_api.set_device_executor(song_bridge.apply_command)
            self.managed_api.register(self.app)
        except Exception as exc:  # noqa: BLE001 - 托管面失败不得拖垮 bridge
            self.managed_api = None
            logger.warning(f"[APIServer] managed playback API unavailable: {exc}")

    def _create_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task:
        """Create a background task and log any unhandled exception."""
        task = asyncio.create_task(coro)

        def _log_task_result(done_task: asyncio.Task):
            try:
                done_task.result()
            except Exception as exc:
                logger.error(f"[APIServer] Background task failed ({name}): {exc}")

        task.add_done_callback(_log_task_result)
        return task

    async def start(self):
        """Start the HTTP server"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(f"[APIServer] HTTP server started at http://{self.host}:{self.port}")
        # ticket 12：启动时声明业务 token 模式（labels 非敏感，供运维核对；
        # 配置非法在此 ERROR 一次——运行期每个请求的 503 会再记）
        tokens = configured_tokens()
        if tokens.errors:
            logger.error("[APIServer] 业务 token 配置非法：" + "; ".join(tokens.errors))
        elif tokens.entries:
            logger.info(f"[APIServer] 业务 token labels={','.join(tokens.labels)}")
        if self.inject_enabled:
            # 注入流式中继：下游 TCP（hook 拉流）端口 = HTTP 端口 + 1
            await self.inject_relay.start(self.host, self.port + 1)
            logger.info(
                f"[APIServer] inject 模块已启用（中继 :{self.port + 1} "
                f"auth={self.inject_relay.auth_mode}）"
            )
        else:
            # ticket 07：关闭时不监听 :9093；端点统一 503 code=inject_disabled
            # （GET /api/inject 与 /api/health 仍报告 enabled=false，供运维观测）
            logger.info(
                "[APIServer] inject 模块未启用（inject.enabled=false）："
                "不启动中继端口，注入控制/数据面端点返回 503"
            )
        # B-37：外部文本入口的鉴权模式启动声明（「当前到底有没有鉴权」不靠猜；
        # 本端点无只读探针，观测面 = 本日志行）
        if resolve_external_text_enabled(self.config):
            cidrs = resolve_external_text_cidrs()
            mode = "refused" if cidrs.refused else f"{len(cidrs.networks)} networks"
            logger.info(
                f"[APIServer] external_text 模块已启用（限源={mode} "
                f"backend={resolve_external_text_backend(self.config)}）"
            )
        else:
            logger.info(
                "[APIServer] external_text 模块未启用（external_text.enabled=false）："
                "端点返回 503 code=external_text_disabled"
            )
        # 票 23：历史点击 worker（设备 spool → 9102 clickpoll → song_request M5 点播）。
        # enabled=False 不启动；开/关 = 配置后重启服务（与其他 CCH 开关同语义）。
        try:
            from core.music import cch_click

            if cch_click.enabled():
                self._cch_click_task = asyncio.create_task(cch_click.run_worker())
                logger.info("[APIServer] cch_click worker started（cch_click_enabled=true）")
            else:
                logger.info("[APIServer] cch_click worker 未启用（cch_click_enabled=false）")
        except Exception as exc:  # noqa: BLE001 - worker 启动失败不影响 HTTP 服务
            logger.error(f"[APIServer] cch_click worker start failed: {type(exc).__name__}")

    async def stop(self):
        """Stop the HTTP server"""
        self._cancel_inject_expiry()
        task, self._cch_click_task = self._cch_click_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.inject_relay.stop()
        if self.runner:
            await self.runner.cleanup()
            logger.info("[APIServer] HTTP server stopped")

    # ============ Handlers ============

    @staticmethod
    def _remote_peer(request: web.Request) -> str | None:
        """原始 TCP peer（默认 IP 事实源；`X-Forwarded-For` 不在此读）。

        `request.remote` 缺失时回退 transport peer（兼容测试替身/非标准请求）。
        """
        remote = getattr(request, "remote", None)
        if not remote:
            transport = getattr(request, "transport", None)
            peer = transport.get_extra_info("peername") if transport else None
            if peer:
                remote = peer[0] if isinstance(peer, tuple) and peer else str(peer)
        return remote

    def _caller_tag(self, request: web.Request) -> str:
        """返回调用方标识（用于审计“是谁让音箱说话”）。incident §5 建议 7。

        **默认只认 TCP peer**（`request.remote`）——生产是 host 网络直连、无反向代理，
        `X-Forwarded-For` / `X-Real-IP` 可被任意客户端伪造，默认信任等于审计失效。
        仅在显式 `TRUST_PROXY=1`（部署在受控反代之后）时才读代理链头。
        判定逻辑见 `core/services/caller.py`（纯函数，可单测）。
        """
        return resolve_caller_tag(
            self._remote_peer(request),
            getattr(request, "headers", None),
            trust_proxy=trust_proxy_enabled(),
        )

    @staticmethod
    def _redact_text(text: str, limit: int = 40) -> str:
        """文本摘要（脱敏）：仅截断，不写完整内容到日志。"""
        t = (text or "").strip().replace("\n", " ")
        return t[:limit] + ("…" if len(t) > limit else "")

    async def handle_play_text(self, request: web.Request) -> web.Response:
        """
        POST /api/play/text
        Play text via TTS

        Request body:
            {
                "text": "你好",           # required
                "blocking": false,        # optional, default false
                "timeout": 60000          # optional, timeout in ms
            }

        P0 ticket 02：全路径走 `speaker.announce`（双闸门 + 词表安全检查 +
        完成等待）。文案命中唤醒/指令词表时拒绝播报（blocking 返回 400
        code=announce_rejected；非阻塞在后台任务中拒绝并记日志）。
        """
        try:
            data = await request.json()
            text = data.get("text")

            if not text:
                return web.json_response(
                    {"success": False, "error": "Missing required field: text"},
                    status=400
                )

            blocking = data.get("blocking", False)
            timeout = data.get("timeout", 10 * 60 * 1000)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            # incident-kws-self-trigger-loop.md §5: 外部播报（HA/企微/DDNS）经此端点发声，
            # 必须走闸门编排，否则就是潜在的自触发源（本次事故的真正入口）。
            # 建议 7：记录调用方，回答“是谁让音箱说话的”。
            logger.info(
                f"[APIServer] /api/play/text caller={self._caller_tag(request)} "
                f"blocking={blocking} text={self._redact_text(text)!r}"
            )

            if blocking:
                try:
                    async with speaker.announce(
                        text=text,
                        reason="api/play/text blocking",
                        gate_seconds=self._mic_gate_seconds(timeout),
                        wait=False,  # 阻塞播放内含等完成，避免双重等待
                    ):
                        result = await speaker.play(
                            text=text, blocking=True, timeout=timeout
                        )
                except AnnounceRejected as e:
                    return web.json_response(
                        {"success": False, "error": str(e), "code": "announce_rejected"},
                        status=400,
                    )
                return web.json_response({"success": result})
            else:
                # 非阻塞：播放命令立即返回；announce 在后台任务里完成
                # 「双闸门 → 播放 → 等真正播完（超时兜底）→ 双闸门归还」。
                async def _announce_in_background():
                    async with speaker.announce(
                        text=text, reason="api/play/text background"
                    ):
                        await speaker.play(
                            text=text, blocking=False, timeout=timeout
                        )

                self._create_background_task(
                    _announce_in_background(), "play/text announce"
                )
                return web.json_response({"success": True, "message": "Playing text in background"})

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(f"[APIServer] Error playing text: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_play_url(self, request: web.Request) -> web.Response:
        """
        POST /api/play/url
        Play audio from URL

        Request body:
            {
                "url": "http://example.com/audio.mp3",  # required
                "blocking": false,                       # optional, default false
                "timeout": 60000                         # optional, timeout in ms
            }
        """
        try:
            data = await request.json()
            url = data.get("url")

            if not url:
                return web.json_response(
                    {"success": False, "error": "Missing required field: url"},
                    status=400
                )

            blocking = data.get("blocking", False)
            timeout = data.get("timeout", 10 * 60 * 1000)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            # incident §5: 任何外部播报都需闸门编排（经 speaker.announce，P0 ticket 03）
            # 建议 7：记录调用方
            logger.info(
                f"[APIServer] /api/play/url caller={self._caller_tag(request)} "
                f"blocking={blocking} url={self._redact_text(url, 60)!r}"
            )

            if blocking:
                try:
                    async with speaker.announce(
                        text=None,  # URL 内容不可做词表检查
                        reason="api/play/url blocking",
                        gate_seconds=self._mic_gate_seconds(timeout),
                        wait=False,  # 阻塞播放内含等完成
                    ):
                        result = await speaker.play(
                            url=url, blocking=True, timeout=timeout
                        )
                except AnnounceRejected as e:
                    return web.json_response(
                        {"success": False, "error": str(e), "code": "announce_rejected"},
                        status=400,
                    )
                return web.json_response({"success": result})
            else:
                async def _announce_url_in_background():
                    # 预算沿用改前口径：闸门上限 125s、完成等待 120s。
                    async with speaker.announce(
                        text=None,
                        reason="api/play/url background",
                        gate_seconds=125.0,
                        wait_budget_ms=120_000.0,
                    ):
                        await speaker.play(
                            url=url, blocking=False, timeout=timeout
                        )

                self._create_background_task(
                    _announce_url_in_background(), "play/url announce"
                )
                return web.json_response({"success": True, "message": "Playing URL in background"})

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(f"[APIServer] Error playing URL: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_play_file(self, request: web.Request) -> web.Response:
        """
        POST /api/play/file
        Upload and play audio file directly via audio buffer

        Request: multipart/form-data
            - file: audio file (required, mp3/wav/opus etc.)

        Query params:
            - blocking: true/false (optional, default false)
            - sample_rate: target sample rate in Hz (optional, default 24000, can be 48000, 44100, etc.)

        Response:
            {
                "success": true,
                "message": "File played"
            }
        """
        try:
            # Parse query params
            blocking = request.query.get("blocking", "false").lower() == "true"
            sample_rate = int(request.query.get("sample_rate", "24000"))

            reader = await request.multipart()

            # Get the file field
            field = await reader.next()
            if not field or field.name != "file":
                return web.json_response(
                    {"success": False, "error": "Missing required field: file"},
                    status=400
                )

            # Check filename
            filename = field.filename
            if not filename:
                return web.json_response(
                    {"success": False, "error": "No filename provided"},
                    status=400
                )

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            suffix = os.path.splitext(filename)[1] or ".mp3"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                total_size = 0
                while True:
                    chunk = await field.read_chunk(size=8192)
                    if not chunk:
                        break
                    temp_file.write(chunk)
                    total_size += len(chunk)
                temp_path = temp_file.name

            logger.info(f"[APIServer] Received file: {filename}, size: {total_size} bytes, blocking={blocking}, sample_rate={sample_rate}")
            # 建议 7：记录调用方（与 /api/play/text|url 一致）
            logger.info(f"[APIServer] /api/play/file caller={self._caller_tag(request)}")
            logger.info(f"[APIServer] Saved upload to temp file: {temp_path}")

            async def play_audio():
                # incident §5: 文件播报也走音箱功放（闸门编排经 speaker.announce，P0 ticket 03；
                # 文件内容不可做词表检查，text=None）
                try:
                    async with speaker.announce(
                        text=None,
                        reason="api/play/file",
                        gate_seconds=305.0,
                        wait=False,  # play_server_file 阻塞到播完
                    ):
                        success = await speaker.play_server_file(
                            temp_path,
                            blocking=True,
                            sample_rate=sample_rate,
                        )
                    if success:
                        logger.info(f"[APIServer] Finished playing: {filename}")
                    else:
                        logger.error(f"[APIServer] Error playing file: {filename}")
                    return success
                finally:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)

            if blocking:
                # Wait for playback to complete
                success = await play_audio()
                return web.json_response({
                    "success": success,
                    "message": f"Finished playing: {filename}",
                    "filename": filename,
                    "size": total_size,
                    "sample_rate": sample_rate
                })
            else:
                # Run in background
                asyncio.create_task(play_audio())
                return web.json_response({
                    "success": True,
                    "message": f"Playing file: {filename}",
                    "filename": filename,
                    "size": total_size,
                    "sample_rate": sample_rate
                })

        except Exception as e:
            logger.error(f"[APIServer] Error handling file upload: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_get_status(self, request: web.Request) -> web.Response:
        """
        GET /api/status
        Get current speaker status
        """
        try:
            speaker = get_speaker()

            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            status = await speaker.get_playing()

            return web.json_response({
                "success": True,
                "data": {
                    "status": status  # "playing", "paused", "idle"
                }
            })

        except Exception as e:
            logger.error(f"[APIServer] Error getting status: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_wakeup(self, request: web.Request) -> web.Response:
        """
        POST /api/wakeup
        Wake up the speaker

        Request body:
            {
                "silent": false   # optional, default false (audible wakeup)
            }
        """
        try:
            data = await request.json() if request.can_read_body else {}
            silent = data.get("silent", False)

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            result = await speaker.wake_up(awake=True, silent=silent)
            return web.json_response({"success": result})

        except Exception as e:
            logger.error(f"[APIServer] Error waking up: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_stop(self, request: web.Request) -> web.Response:
        """
        POST /api/interrupt
        Interrupt current playback
        """
        try:
            speaker = get_speaker()
            xiaoai = get_xiaoai()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            await speaker.stop_device_audio()
            # 停止连续对话
            if xiaoai:
                xiaoai.stop_conversation()

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"[APIServer] Error interrupting: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_audio_input(self, request: web.Request) -> web.Response:
        """
        POST /api/audio_input
        T7.6 恢复通道：解除「停止聆听」隐私开关（硬件麦克风恢复 + KWS 恢复分析）。
        一旦用户喊「停止聆听」，语音通道自关，本端点是唯一可靠的恢复路径。
        请求体（可选）：{"text": "..."} 预留给未来"经 HTTP 喂入音频/文本"场景，
        本期仅作恢复开关使用。

        Response:
            {"success": true, "mic": "on"/"off", "listening": bool}
        """
        try:
            speaker = get_speaker()
            kws = get_kws()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503,
                )

            # 解除停止聆听：硬件麦克风恢复
            try:
                await speaker.set_mic(True)
            except Exception as e:
                logger.warning(f"[APIServer] set_mic(True) on audio_input failed: {e}")

            # T7.6 修复：设备端静音标志 /tmp/mipns/mute 由 event:7（闭麦）创建、
            # 由 event:8（开麦）删除；上面 set_mic(True) 发出的正是 event:8。
            # 这里再显式 rm 一次是兜底：历史版本 set_mic 语义曾反向（on=True 发
            # event:7），若设备上残留旧标志，宿主会一直静音、KWS 收不到语音、
            # 恢复后④⑤ 无响应。与「停止聆听」时对称，确保真正恢复采集。
            try:
                await speaker.clear_mic_mute_flag()
            except Exception as e:
                logger.warning(f"[APIServer] rm /tmp/mipns/mute on audio_input failed: {e}")

            # KWS 恢复关键词分析
            if kws:
                kws.enable_listening()
            logger.info("[APIServer] /api/audio_input 恢复: mic unmuted + KWS listening enabled")

            mic = "off"
            try:
                mic = await speaker.get_mic()
            except Exception:
                pass

            return web.json_response({
                "success": True,
                "mic": mic,
                "listening": not (kws and kws.is_listening_disabled()),
            })
        except Exception as e:
            logger.error(f"[APIServer] Error in audio_input: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500,
            )

    def _cancel_inject_expiry(self) -> None:
        """取消上一次注入窗口的 TTL 定时器（开新窗/关窗/停机时调用）。"""

        task = self._inject_expire_task
        if task is not None and not task.done():
            task.cancel()
        self._inject_expire_task = None

    def _schedule_inject_expiry(self, remaining: float) -> None:
        """窗口 TTL 到点主动关闸 + 关中继；不依赖下一次 ASR/health 的懒判定。"""

        self._cancel_inject_expiry()

        async def _expire() -> None:
            try:
                await asyncio.sleep(max(1.0, float(remaining)))
                if EventManager.injection_gate_active():
                    EventManager.close_injection_gate()
                    self.inject_relay.close_session("ttl expiry")
                    logger.warning("[APIServer] 注入窗口 TTL 到期，已自动关窗")
            except asyncio.CancelledError:
                raise

        self._inject_expire_task = asyncio.create_task(_expire())

    @staticmethod
    def _truthy_flag(value) -> bool:
        """严格解析 JSON 布尔：字符串 "false"/"0" 不得被当成真值。"""

        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return False

    @staticmethod
    def _parse_inject_duration(raw) -> float | None:
        """解析开窗时长：None/非法 → 默认 TTL；合法值夹在 [1, INJECTION_GATE_TTL]。"""
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return min(max(value, 1.0), EventManager.INJECTION_GATE_TTL)

    def _inject_health(self) -> dict:
        """健康接口的 inject 段（不泄露凭证/识别文本）。"""
        if not self.inject_enabled:
            return {
                "enabled": False,
                "gate": "closed",
                "remaining": 0.0,
                "relay": {"active": False},
            }
        gate_open = EventManager.injection_gate_active()
        return {
            "enabled": True,
            "gate": "open" if gate_open else "closed",
            "remaining": round(EventManager.injection_gate_remaining(), 1),
            "relay": {
                "active": self.inject_relay.active,
                # 数据面鉴权模式（ticket 12）：cidr/anon/refused；unknown=尚未 start
                "auth": self.inject_relay.auth_mode,
                "stats": dict(self.inject_relay.stats),
            },
        }

    async def handle_inject(self, request: web.Request) -> web.Response:
        """
        POST /api/inject
        PC 侧 ASR 注入窗口控制（firmware-asr-injection-feasibility.md §3.4/§3.5）。

        开窗 = 开注入闸门（机制 A）+ 开数据面中继 + P2 静默唤醒引擎
        （pnshelper event_notify）。唤醒后引擎 enable asr=1，云端 ASR 才会出流；
        闸门保证识别结果不会被当成用户指令交给 Agent/对话后端。

        **原子性（ticket 07）**：顺序固定为「开闸门 → 开中继 → 唤醒引擎」，
        任一步失败（含 wake_up 返 False/异常）都整窗回滚（关闸门 + 关中继）
        并返回 503，不留下半开状态。

        模块关闭（inject.enabled=false）时本端点 503 + code=inject_disabled。

        请求体：
            {"on": true,  "duration": 30}   # 开窗（duration 可选，默认/上限见 TTL）
            {"on": false}                    # 关窗（PC finally 必须调用；幂等）

        Response:
            {"success": true, "enabled": true, "gate": "open"/"closed", "remaining": <秒>}
        """
        if not self.inject_enabled:
            logger.info(
                f"[APIServer] /api/inject 拒绝 caller={self._caller_tag(request)}："
                "inject 模块未启用"
            )
            return InjectRelay._disabled_response()

        # 数据面被拒（auth=refused）时不开窗（fail closed，503 + 明确原因）：
        # 窗口 = 闸门 + 中继 + 唤醒，中继未监听则注入无从发生（notes/10 §3）
        data_auth = self.inject_relay.data_auth
        if data_auth is not None and data_auth.mode == "refused":
            logger.error(
                f"[APIServer] /api/inject 拒绝开窗 caller={self._caller_tag(request)}："
                f"数据面未启用鉴权（{data_auth.reason}）"
            )
            return web.json_response(
                {
                    "success": False,
                    "enabled": True,
                    "code": "inject_data_refused",
                    "error": f"9093 数据面拒绝监听，注入不可用：{data_auth.reason}",
                },
                status=503,
            )

        try:
            data = await request.json() if request.can_read_body else {}
            caller = self._caller_tag(request)

            if self._truthy_flag(data.get("on")):
                duration = self._parse_inject_duration(data.get("duration"))
                result = await open_injection_window(
                    EventManager,
                    self.inject_relay,
                    get_speaker(),
                    duration,
                )
                if not result.ok:
                    logger.warning(
                        f"[APIServer] /api/inject 开窗失败并已回滚 caller={caller} "
                        f"code={result.code} error={result.error}"
                    )
                    return web.json_response(
                        {
                            "success": False,
                            "enabled": True,
                            "gate": "closed",
                            "remaining": 0,
                            "code": result.code,
                            "error": result.error,
                            "rolled_back": result.rolled_back,
                        },
                        status=503,
                    )

                self._schedule_inject_expiry(result.remaining)
                logger.info(
                    f"[APIServer] /api/inject 开窗 caller={caller} "
                    f"ttl={(result.ttl or 0):.0f}s wake_up={result.wakeup}"
                )
                return web.json_response({
                    "success": True,
                    "enabled": True,
                    "gate": "open",
                    "remaining": result.remaining,
                    "ttl": result.ttl,
                    "wakeup": result.wakeup,
                })

            # 关窗幂等：未开窗时重复调用同样返回 closed，不报错。
            EventManager.close_injection_gate()
            self._cancel_inject_expiry()
            self.inject_relay.close_session("api close")   # 数据面：断下游 + 丢上行
            logger.info(f"[APIServer] /api/inject 关窗 caller={caller}")
            return web.json_response({
                "success": True,
                "enabled": True,
                "gate": "closed",
                "remaining": 0,
            })

        except Exception as e:
            logger.error(f"[APIServer] Error in inject: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500,
            )

    async def handle_inject_status(self, request: web.Request) -> web.Response:
        """
        GET /api/inject
        注入闸门状态观测（机制 A 的可观测性要求）。

        这是**只读探针**，在模块关闭时仍返回 200 + `enabled=false`（唯一的
        关闭态例外）：让运维能区分「模块没开」与「窗口没开」，与 /api/health
        的 inject.enabled 同义；其余注入端点关闭时一律 503。
        """
        if not self.inject_enabled:
            return web.json_response({
                "success": True,
                "enabled": False,
                "gate": "closed",
                "remaining": 0,
                "relay": {"active": False},
            })
        return web.json_response({
            "success": True,
            "enabled": True,
            "gate": "open" if EventManager.injection_gate_active() else "closed",
            "remaining": EventManager.injection_gate_remaining(),
            "relay": {
                "active": self.inject_relay.active,
                "auth": self.inject_relay.auth_mode,
                "stats": dict(self.inject_relay.stats),
            },
        })

    async def handle_external_text(self, request: web.Request) -> web.Response:
        """
        POST /api/external/text
        外部 ASR 文本入口（B-37）：把一段已被某 ASR 识别完成的文本当作一轮
        用户输入送进对话管线，执行结果走既有出口（LX06 TTS / 工具 / HA）。

        闸门（fail closed）。**写类 Bearer 鉴权由 `api_auth_middleware` 先行**
        （缺 token 401 / 错 token 403 / 未配置 503，均在进 handler 前拒绝）；
        handler 内顺序判定：
          1. 模块开关 `external_text.enabled`（env `EXTERNAL_TEXT_ENABLE` 覆盖）
             —— 关闭一律 503 code=external_text_disabled；
          2. 限源 `EXTERNAL_TEXT_ALLOW_CIDR`（默认空 = refused；按**原始 TCP
             peer** 比对，`X-Forwarded-For` 默认不可信）—— 未配/非法/未命中
             一律 503 code=external_text_refused；
          3. 载荷校验（text 必填 ≤500 字符 / source ≤64 截断 / silent 严格解析）
             —— 400；
          4. 同调用方（token label + TCP peer）最小间隔限流 —— 429
             code=external_text_rate_limited（双麦互听回环缓解）；
          5. 后端登记检查（503 backend_unknown）+ 已有活跃会话 → 409
             code=external_text_busy（串行拒绝，不顶替）→ 占位异常 503
             code=external_text_start_failed。

        请求体：
            {"text": "...",           # 必填，≤500 字符
             "source": "cch-screen",  # 可选调用方 id（仅进审计，可伪造——
                                      #  鉴权/限流不依赖它）
             "silent": false}         # 可选：只执行不播报（回复文本随响应返回）

        Response:
            非 silent：202 {"success": true, "accepted": true}（轮在后台执行，
                       回复走 LX06 TTS）
            silent：   200 {"success": true, "completed": true, "reply": "...",
                       "outcome": "...", "turn": N}（等待本轮完成后返回）
        """
        caller = self._caller_tag(request)

        # 1. 模块开关（fail closed）
        if not resolve_external_text_enabled(self.config):
            logger.info(
                f"[APIServer] /api/external/text 拒绝 caller={caller}：模块未启用"
            )
            return web.json_response(
                {
                    "success": False,
                    "code": EXTERNAL_TEXT_DISABLED_CODE,
                    "error": "外部文本入口未启用（external_text.enabled=false，"
                    "或环境变量 EXTERNAL_TEXT_ENABLE=0）",
                },
                status=503,
            )

        # 2. 限源（fail closed：未配 / 非法 / 未命中一律拒；peer 用原始 TCP）
        cidrs = resolve_external_text_cidrs()
        peer_ip = self._remote_peer(request)
        if cidrs.refused or not peer_allowed(peer_ip, cidrs.networks):
            reason = cidrs.refused_reason or "源不在 EXTERNAL_TEXT_ALLOW_CIDR 白名单"
            logger.warning(
                f"[APIServer] /api/external/text 拒绝 caller={caller} peer={peer_ip}: {reason}"
            )
            return web.json_response(
                {
                    "success": False,
                    "code": EXTERNAL_TEXT_REFUSED_CODE,
                    "error": f"限源拒绝：{reason}",
                },
                status=503,
            )

        # 3. 载荷校验（token 鉴权已在中间件完成）
        try:
            data = await request.json() if request.can_read_body else {}
        except Exception:
            return web.json_response(
                {"success": False, "error": "请求体不是合法 JSON"}, status=400
            )
        payload, error = validate_external_text_payload(data)
        if payload is None:
            return web.json_response({"success": False, "error": error}, status=400)

        # 4. per-caller 限流（回环缓解；键 = token label + TCP peer，均不可伪造）。
        # 注：槽位在本步写入、先于 busy 判定——busy 409 的请求同样占用窗口
        # （方向上加强回环缓解：语音会话刚结束的连续重试会被挡到窗口结束）。
        try:
            min_interval = float(
                self.config.get_app_config("external_text.min_interval", 10) or 0
            )
        except (TypeError, ValueError):
            min_interval = 10.0
        label = request.get("auth_label", "") if hasattr(request, "get") else ""
        throttle_key = f"{label or 'none'}@{peer_ip}"
        if not self._external_text_throttle.try_acquire(
            throttle_key, now=time.monotonic(), min_interval=min_interval
        ):
            logger.warning(
                f"[APIServer] /api/external/text 限流 caller={caller} "
                f"label={label or 'none'} peer={peer_ip} min_interval={min_interval}s"
            )
            return web.json_response(
                {
                    "success": False,
                    "code": EXTERNAL_TEXT_RATE_LIMITED_CODE,
                    "error": f"同一调用方 {min_interval:.0f}s 内至多一轮",
                },
                status=429,
            )

        # 5. 后端登记检查 + 同步占位（原子段：busy 判定与注册之间无 await）
        backend = resolve_external_text_backend(self.config)
        from core.backend_registry import backend_registry

        if backend_registry.get(backend) is None:
            logger.error(
                f"[APIServer] /api/external/text 后端未登记 backend={backend}"
            )
            return web.json_response(
                {
                    "success": False,
                    "code": EXTERNAL_TEXT_BACKEND_UNKNOWN_CODE,
                    "error": f"配置的后端 {backend} 不在注册表",
                },
                status=503,
            )
        try:
            controller = EventManager.begin_external_text_session(
                backend,
                text=payload.text,
                source=payload.source,
                silent=payload.silent,
            )
        except Exception as exc:
            # 占位段异常（如控制器构造失败；KWS 已由 begin 内部补恢复）：
            # 不裸 500，fail closed 给明确错误码。
            logger.error(
                f"[APIServer] /api/external/text 占位失败 backend={backend} "
                f"caller={caller}: {type(exc).__name__}: {exc}"
            )
            return web.json_response(
                {
                    "success": False,
                    "code": EXTERNAL_TEXT_START_FAILED_CODE,
                    "error": "会话占位失败（内部错误），已放弃本轮",
                },
                status=503,
            )
        if controller is None:
            logger.info(
                f"[APIServer] /api/external/text busy（已有活跃会话）caller={caller}"
            )
            return web.json_response(
                {
                    "success": False,
                    "code": EXTERNAL_TEXT_BUSY_CODE,
                    "error": "已有活跃对话会话（串行拒绝，不顶替）；稍后重试",
                },
                status=409,
            )
        logger.info(
            f"[APIServer] /api/external/text accept caller={caller} label={label or 'none'} "
            f"backend={backend} source={payload.source or 'unknown'} "
            f"len={len(payload.text)} silent={payload.silent}"
        )

        if payload.silent:
            # 等待本轮完成、随响应返回回复文本（CCH 屏显场景）；无服务端
            # 超时——LLM/播放各自有界，客户端超时自理（超时后轮继续执行）。
            result = await EventManager.finish_external_text_session(backend, controller)
            return web.json_response(
                {
                    "success": True,
                    "completed": True,
                    "reply": result.get("reply", ""),
                    "outcome": result.get("outcome", ""),
                    "turn": result.get("turn_id"),
                }
            )

        # 非 silent：轮在后台执行（回复走 LX06 TTS）；202 即刻返回，
        # 端到端延迟口径（≤5s）只覆盖「提交→开始应答」。
        self._create_background_task(
            EventManager.finish_external_text_session(backend, controller),
            name="external-text-finish",
        )
        return web.json_response({"success": True, "accepted": True}, status=202)

    async def handle_external_np(self, request: web.Request) -> web.Response:
        """POST /api/external/np —— CCH 音乐事件上行（票 03 编排面，2026-11-20）。

        写类 Bearer 由 `api_auth_middleware` 先行（worker 用 BRIDGE_TOKEN 同源）；
        限源复用 `EXTERNAL_TEXT_ALLOW_CIDR`（capture 容器 10.10.10.2 已在白名单）。

        请求体：{"kind": "META"|"STATE", "extras": {...}, "device_ts": "..."}
        编排动作（core/music/cch_np.py）：STATE finish/stop → 换代 cast 下一首；
        suspend → 记录恢复点；其余 recorded。双关（np_enabled=false）时只记录不编排。
        """
        caller = self._caller_tag(request)
        cidrs = resolve_external_text_cidrs()
        peer_ip = self._remote_peer(request)
        if cidrs.refused or not peer_allowed(peer_ip, cidrs.networks):
            logger.warning(f"[APIServer] /api/external/np 拒绝 caller={caller} peer={peer_ip}")
            return web.json_response(
                {"success": False, "code": "np_refused", "error": "限源拒绝"},
                status=503,
            )
        try:
            n = int(request.headers.get("Content-Length") or 0)
            if n <= 0 or n > 32768:
                raise ValueError("bad size")
            body = json.loads(await request.read())
        except Exception:
            return web.json_response(
                {"success": False, "code": "np_bad_payload", "error": "载荷非法"}, status=400)
        kind = str(body.get("kind") or "")
        if kind not in ("META", "STATE"):
            return web.json_response(
                {"success": False, "code": "np_bad_kind", "error": "kind must be META|STATE"},
                status=400,
            )
        extras = body.get("extras") or {}
        device_ts = str(body.get("device_ts") or "")
        try:
            from core.music.cch_np import np_enabled as np_gate
            from core.music import cch_np

            if not np_gate():
                # 双关：只记录不编排（META/isFavorite 仍进内存——票 13 收藏幂等的读态源）
                cch_np.record_event(kind, extras, device_ts)
                logger.info(f"[APIServer] /api/external/np 记录（编排关闭）caller={caller}")
                return web.json_response({"success": True, "action": "recorded_only"})
            action = await cch_np.on_np_event(kind, extras, device_ts)
            logger.info(f"[APIServer] /api/external/np kind={kind} action={action['action']} caller={caller}")
            return web.json_response({"success": True, "action": action.get("action", "recorded")})
        except Exception as exc:  # noqa: BLE001 - 编排异常不阻塞 worker
            logger.error(f"[APIServer] /api/external/np 编排异常: {type(exc).__name__}: {exc}")
            return web.json_response(
                {"success": False, "code": "np_orchestration_error",
                 "error": type(exc).__name__}, status=500,
            )

    async def handle_cch_bg(self, request: web.Request) -> web.StreamResponse:
        """GET /api/cch/bg/{name} —— 点歌虚化背景图（只读；文件名 = 封面 sha1 前 16 位）。

        设备侧 <img> 直取，不能带 Bearer；名字不可猜（sha1 前缀）且只读，风险等同公开封面。
        """
        name = str(request.match_info.get("name") or "")
        if not re.fullmatch(r"[0-9a-f]{16}\.jpg", name):
            return web.Response(status=404)
        from core.music import bg_cache

        path = bg_cache.file_path(name[:-4])
        if not os.path.exists(path):
            return web.Response(status=404)
        resp = web.FileResponse(path)
        resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
        return resp

    async def handle_cch_collect(self, request: web.Request) -> web.Response:
        """POST /api/cch/collect —— CCH 收藏（票 13；2026-11-22 增强 playlist_id 选歌单）。

        body = {"collect": bool, "playlist_id"?: int}：
        - collect=false → 取消；playlist_id 缺省 = 内置收藏歌单（票 13 默认行为）。
        - playlist_id 指定时校验「存在且可写」（type=normal，radio 只读拒）；
          非法/不存在/不可写 → 400，不新建歌单（新建仅手机 App）。
        幂等与写落点由 cch_np.collect_favorite 统一（Songloft 单落点，不回退原厂 tap）。
        写类 Bearer 由 `api_auth_middleware` 先行（proxy 转发用 BRIDGE_TOKEN 同源）。
        """
        caller = self._caller_tag(request)
        try:
            n = int(request.headers.get("Content-Length") or 0)
            if n <= 0 or n > 512:
                raise ValueError("bad size")
            body = json.loads(await request.read())
            intent = body.get("collect")
            if not isinstance(intent, bool):
                raise ValueError("collect must be boolean")
            playlist_id = body.get("playlist_id")
            if playlist_id is not None:
                playlist_id = int(playlist_id)   # bool 也被 int() 接受，下面范围校验挡掉
                if isinstance(body.get("playlist_id"), bool) or playlist_id <= 0:
                    raise ValueError("playlist_id must be positive int")
        except Exception:
            return web.json_response(
                {"success": False, "code": "collect_bad_payload",
                 "error": "载荷非法：{\"collect\": true|false, \"playlist_id\"?: int>0}"}, status=400)
        # 校验歌单存在且可写（仅指定时）；复用 songloft_api 统一门面读歌单列表。
        if playlist_id is not None:
            try:
                import aiohttp

                from core.music import songloft_api

                async with aiohttp.ClientSession() as http:
                    writable = await songloft_api.writable_playlist_ids(http=http)
                if playlist_id not in writable:
                    return web.json_response(
                        {"success": False, "code": "playlist_not_writable",
                         "error": f"歌单 {playlist_id} 不存在或不可写"}, status=400)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[APIServer] /api/cch/collect playlist 校验失败: "
                             f"{type(exc).__name__}: {exc}")
                return web.json_response(
                    {"success": False, "code": "playlist_check_error",
                     "error": type(exc).__name__}, status=502)
        try:
            from core.music import cch_np

            result = await cch_np.collect_favorite(intent, playlist_id=playlist_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[APIServer] /api/cch/collect 异常: {type(exc).__name__}: {exc}")
            return web.json_response(
                {"success": False, "code": "collect_error",
                 "error": type(exc).__name__}, status=500)
        action = result.get("action")
        logger.info(f"[APIServer] /api/cch/collect action={action} "
                    f"playlist={playlist_id} caller={caller}")
        if action == "unknown_state":
            return web.json_response(
                {"success": False, "code": "favorite_state_unknown",
                 "error": "收藏态未知（数据面未同步 isFavorite），拒绝盲发翻转"}, status=409)
        if action == "tap_failed":
            return web.json_response(
                {"success": False, "code": "tap_failed",
                 "detail": result.get("detail", "")}, status=502)
        return web.json_response({"success": True, "action": action,
                                  "favorite": result.get("favorite"),
                                  "playlist_id": result.get("playlist_id")})

    async def handle_livez(self, request: web.Request) -> web.Response:
        """GET /livez —— 进程存活（不看设备；不走 /api/* 鉴权）。"""
        from core.services.health import livez_payload

        return web.json_response(livez_payload())

    async def handle_readyz(self, request: web.Request) -> web.Response:
        """GET /readyz —— 能否承接语音/设备命令（liveness 与 readiness 分离）。"""
        from core.services.health import readyz_payload

        recording_expected = None
        try:
            from core.xiaoai import XiaoAI

            recording_expected = XiaoAI.audio_health().get("recording_expected")
        except Exception:  # noqa: BLE001 - 就绪探针不得因单段失败整体 500
            recording_expected = None
        status, payload = readyz_payload(
            speaker_ready=get_speaker() is not None,
            recording_expected=recording_expected,
        )
        return web.json_response(payload, status=status)

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        GET /api/health
        Health check endpoint（保留只读、可无鉴权；不暴露凭证/完整文本）

        默认只返回内存态：录音帧龄/策略、播放事件面、注入状态。
        `?probe=1` 时额外采集设备探针（mediaplayer 存活 / 内存 / 麦态 / 固件
        白名单字段），总超时 4s；探针失败不影响整体健康响应。
        """
        audio: dict = {}
        try:
            from core.xiaoai import XiaoAI

            audio = XiaoAI.audio_health()
        except Exception as e:  # noqa: BLE001 - 健康接口不得因单段失败整体 500
            logger.debug(f"[APIServer] health audio snapshot failed: {e}")

        # KWS 可听性（2026-10-28）：唤醒没反应时先分清「闸门/暂停卡住」还是
        # 「音频被音乐盖住」——此前只有命中日志，哑了没有任何观测面。
        try:
            kws = get_kws()
            if kws is not None:
                gate_count, gate_reasons = kws.gate_status()
                audio["kws"] = {
                    "paused": bool(getattr(kws, "paused", False)),
                    "listen_disabled": bool(getattr(kws, "listen_disabled", False)),
                    "gate_count": int(gate_count),
                    "gate_reasons": list(gate_reasons),
                }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[APIServer] health kws snapshot failed: {e}")

        # 点歌队列运行态（只给计数/标志，不给歌名文本）
        try:
            from core.tools import song_request

            audio["song_queue"] = song_request.queue_health()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[APIServer] health song queue snapshot failed: {e}")

        # TTS focus 引用计数（音乐不恢复时区分「没释放」与「释放失败」）
        try:
            from core.music import focus_bridge

            audio["focus"] = focus_bridge.health()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[APIServer] health focus snapshot failed: {e}")

        playback: dict = {}
        try:
            from core.device import get_playback_events

            snap = get_playback_events().snapshot()
            playback = {
                key: snap.get(key)
                for key in (
                    "source",
                    "shadow_sources",
                    "native_cmd_configured",
                    "native_running",
                    "waits",
                    "mismatch_count",
                    "last_result",
                )
            }
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[APIServer] health playback snapshot failed: {e}")

        probe = None
        if request.query.get("probe", "").lower() in ("1", "true", "yes", "on"):
            # 设备探针会打 3 条设备 RPC，不能随免鉴权的 /api/health 一起裸放。
            # 用未登记的合成路径走 write 分类：配置了 token 时必须带 Bearer，
            # 未配置 token 时 503（probe 失败关闭，健康读本身仍可用）。
            decision = authorize(
                "/api/health/probe",
                "GET",
                authorization=request.headers.get("Authorization"),
                tokens=configured_tokens(),
            )
            if not decision.allowed:
                return web.json_response(
                    {
                        "success": False,
                        "error": decision.error,
                        "code": decision.code,
                    },
                    status=decision.status,
                    headers=decision.header_dict(),
                )
            speaker = get_speaker()
            if speaker is None:
                probe = {"speaker": False}
            else:
                async def _collect_probe() -> dict:
                    firmware = None
                    try:
                        firmware = (await speaker.device.firmware_info()).readback or None
                    except Exception:  # noqa: BLE001 - 单段失败不影响其余探针
                        logger.debug("[APIServer] firmware probe failed")
                    return {
                        "mediaplayer_alive": await speaker.mediaplayer_alive(),
                        "memory_free": await speaker.memory_free(),
                        "mic": await speaker.get_mic(),
                        # A-4/P3 票 01：固件健康白名单字段（无 PII），
                        # 排障不再 SSH；失败时置 None（健康读不整体 500）。
                        "firmware": firmware,
                    }

                try:
                    probe = await asyncio.wait_for(_collect_probe(), timeout=4.0)
                except asyncio.TimeoutError:
                    probe = {"error": "timeout"}
                except Exception as e:  # noqa: BLE001
                    probe = {"error": type(e).__name__}

        return web.json_response(
            build_health_payload(
                speaker_ready=get_speaker() is not None,
                audio=audio,
                playback=playback,
                inject=self._inject_health(),
                probe=probe,
            )
        )

    async def handle_tts_doubao(self, request: web.Request) -> web.Response:
        """
        POST /api/tts/doubao
        Synthesize text using Doubao (ByteDance Volcano) TTS and play it

        Request body:
            {
                "text": "你好",                    # required
                "app_id": "your_app_id",           # optional (uses config if not provided)
                "access_key": "your_access_key",   # optional (uses config if not provided)
                "resource_id": "your_resource_id", # optional (auto-detected based on speaker_id)
                "speaker_id": "zh_female_cancan_mars_bigtts",  # optional, default voice
                "speed": 1.0,                       # optional, 0.8-2.0
                "blocking": true,                   # optional, default false
                "emotion": "happy",                 # optional, emotion for multi-emotion speakers
                "context_texts": [                   # optional, only for 2.0 speakers (only first value effective)
                    "你可以说慢一点吗？",
                    "你可以用特别痛心的语气说话吗？",
                    "你能用骄傲的语气来说话吗？"
                ]
            }
        """
        speaker_id = "<unknown>"
        resource_id_for_log = "<unknown>"
        resolved_format = "<unknown>"
        blocking = False
        use_stream = False

        try:
            data = await request.json()
            text = data.get("text")

            if not text:
                return web.json_response(
                    {"success": False, "error": "Missing required field: text"},
                    status=400
                )

            # Get credentials from request or config
            tts_config = self.config.get_app_config("tts.doubao", {})

            app_id = data.get("app_id") or tts_config.get("app_id")
            access_key = data.get("access_key") or tts_config.get("access_key")
            # resource_id is now optional - will be auto-detected based on speaker
            resource_id = data.get("resource_id") or tts_config.get("resource_id")
            resource_id_for_log = resource_id or "<auto>"

            if not all([app_id, access_key]):
                return web.json_response(
                    {"success": False, "error": "Doubao TTS credentials not configured. Provide app_id and access_key in request or config.py"},
                    status=400
                )

            speaker_id = data.get("speaker_id") or data.get("speaker") or tts_config.get("default_speaker") or "zh_female_shuangkuaisisi_moon_bigtts"
            speed = float(data.get("speed", 1.0))
            blocking = data.get("blocking", False)
            context_texts = data.get("context_texts")  # Only supported for 2.0 speakers
            emotion = data.get("emotion")  # Emotion parameter for multi-emotion speakers

            speaker = get_speaker()
            if not speaker:
                return web.json_response(
                    {"success": False, "error": "Speaker not initialized"},
                    status=503
                )

            # Create TTS instance (auto-detects resource_id if not provided)
            tts = DoubaoTTS(
                app_id=app_id,
                access_key=access_key,
                resource_id=resource_id,
                speaker=speaker_id,
            )
            resolved_format = tts.resolve_audio_format(text)
            resource_id_for_log = tts.resource_id
            logger.info(
                f"[APIServer] Doubao TTS: speaker={speaker_id}, resource_id={tts.resource_id}, format={resolved_format}"
            )

            use_stream = tts_config.get("stream", False)
            if use_stream:
                async def play_tts_stream():
                    play_fn = (
                        open_xiaoai_server.tts_stream_play
                        if blocking
                        else open_xiaoai_server.tts_stream_play_background
                    )
                    await play_fn(
                        text,
                        app_id=app_id,
                        access_key=access_key,
                        resource_id=tts.resource_id,
                        speaker=speaker_id,
                        speed=speed,
                        format=resolved_format,
                        sample_rate=24000,
                        emotion=emotion,
                        context_texts=context_texts,
                    )

                if blocking:
                    await play_tts_stream()
                else:
                    await play_tts_stream()
            else:
                async def play_tts_audio():
                    play_fn = (
                        open_xiaoai_server.tts_play
                        if blocking
                        else open_xiaoai_server.tts_play_background
                    )
                    await play_fn(
                        text,
                        app_id=app_id,
                        access_key=access_key,
                        resource_id=tts.resource_id,
                        speaker=speaker_id,
                        speed=speed,
                        format=resolved_format,
                        sample_rate=24000,
                        emotion=emotion,
                        context_texts=context_texts,
                    )
                    logger.debug("[APIServer] Finished playing TTS audio")

                if blocking:
                    try:
                        await play_tts_audio()
                    except Exception as e:
                        return web.json_response(
                            {"success": False, "error": f"TTS playback failed: {str(e)}"},
                            status=500
                        )
                else:
                    await play_tts_audio()

            if blocking:
                return web.json_response({
                    "success": True,
                    "message": f"TTS played: {text[:50]}..." if len(text) > 50 else f"TTS played: {text}",
                    "speaker_id": speaker_id,
                })

            return web.json_response(
                {
                    "success": True,
                    "message": "TTS request accepted for background playback",
                    "speaker_id": speaker_id,
                    "accepted": True,
                    "blocking": False,
                },
                status=202,
            )

        except json.JSONDecodeError:
            return web.json_response(
                {"success": False, "error": "Invalid JSON"},
                status=400
            )
        except Exception as e:
            logger.error(
                f"[APIServer] Doubao TTS failed: "
                f"speaker={speaker_id}, resource_id={resource_id_for_log}, "
                f"format={resolved_format}, blocking={blocking}, stream={use_stream}, "
                f"error={type(e).__name__}: {e}"
            )
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )

    async def handle_tts_voices(self, request: web.Request) -> web.Response:
        """
        GET /api/tts/doubao_voices
        Get available TTS voices for Doubao

        Query params:
            - version: "1.0", "2.0", or "all" (optional, default shows all)
        """
        try:
            tts_config = self.config.get_app_config("tts.doubao", {})
            resource_id = tts_config.get("resource_id", "")

            # Get version from query param or auto-detect from resource_id
            version = request.query.get("version", "all")

            if version == "2.0":
                voices = DoubaoTTS.VOICES_2_0
            elif version == "1.0":
                voices = DoubaoTTS.VOICES_1_0
            else:
                voices = DoubaoTTS.list_voices()
                # Add version info for all voices
                return web.json_response({
                    "success": True,
                    "data": {
                        "provider": "doubao",
                        "resource_id": resource_id,
                        "versions": {
                            "1.0": {
                                "count": len(DoubaoTTS.VOICES_1_0),
                                "description": "豆包语音合成模型1.0",
                                "voices": DoubaoTTS.VOICES_1_0
                            },
                            "2.0": {
                                "count": len(DoubaoTTS.VOICES_2_0),
                                "description": "豆包语音合成模型2.0 - 支持情感变化、指令遵循、ASMR",
                                "voices": DoubaoTTS.VOICES_2_0
                            }
                        },
                        "total_voices": len(voices)
                    }
                })

            return web.json_response({
                "success": True,
                "data": {
                    "provider": "doubao",
                    "version": version,
                    "resource_id": resource_id,
                    "voices": voices,
                    "count": len(voices)
                }
            })
        except Exception as e:
            logger.error(f"[APIServer] Error getting voices: {e}")
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500
            )
