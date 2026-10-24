"""
HTTP API Server for XiaoZhi
Provides endpoints to play text/audio remotely
"""

import asyncio
import json
import os
import tempfile
from collections.abc import Coroutine
from typing import Any

import open_xiaoai_server
from aiohttp import web
from core.device import get_playback_events
from core.ref import get_speaker, get_xiaoai, get_kws
from core.wakeup_session import EventManager
from core.services.admin_api import AdminAPI
from core.services.auth import authorize, configured_token
from core.services.health import build_health_payload
from core.services.inject_control import (
    INJECT_DISABLED_CODE,
    INJECT_DISABLED_ERROR,
    open_injection_window,
    resolve_inject_enabled,
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
        self._downstream_task: asyncio.Task | None = None
        self.text_queues: list[asyncio.Queue] = []   # 文本回传订阅者（WS，多客户端广播）
        self._text_log: list[tuple[int, str]] = []   # 有界文本日志 [(seq, text)]，不随窗口清
        self._seq: int = 0                            # 文本序号，单调递增，永不复位
        self._window_start_seq: int = 0               # 最近一次 open_session 时的 seq
        self.stats = {"up_bytes": 0, "down_bytes": 0, "sessions": 0, "texts": 0}

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
        """hook 下行 TCP 服务端（:9093）：单客户端，连接后持续推送缓冲中的 PCM。"""
        async def client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            peer = writer.get_extra_info("peername")
            logger.info(f"[InjectRelay] 下游 hook 已连接: {peer}")
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
                self.downstream_writer = None
                try:
                    writer.close()
                except Exception:
                    pass
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
        self._downstream_task = asyncio.create_task(self.downstream_server(host, port))

    async def stop(self) -> None:
        self.close_session("relay stop")
        if self._downstream_task:
            self._downstream_task.cancel()

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
            middlewares=[self.api_auth_middleware, self.admin_api.auth_middleware]
        )
        self.runner = None
        self.site = None
        # 注入模块总开关（ticket 07）：默认关闭；env INJECT_ENABLE 可覆盖 config。
        # 关闭时中继不监听 :9093，控制/数据面端点 fail closed（见 inject_control 文档）。
        self.inject_enabled = resolve_inject_enabled(self.config)
        self.inject_relay = InjectRelay(enabled=self.inject_enabled)   # 注入流式中继（数据面）
        self._inject_expire_task: asyncio.Task | None = None
        global _relay_singleton
        _relay_singleton = self.inject_relay
        self._setup_routes()
        self.admin_api.register(self.app)

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
        decision = authorize(
            path,
            request.method,
            authorization=request.headers.get("Authorization"),
            expected_token=configured_token(),
        )
        if decision.allowed:
            return await handler(request)
        logger.warning(
            f"[APIServer] 拒绝 {request.method} {path} "
            f"caller={self._caller_tag(request)} code={decision.code}"
        )
        return web.json_response(
            {"success": False, "error": decision.error, "code": decision.code},
            status=decision.status,
            headers=decision.header_dict(),
        )

    def _estimate_gate_seconds(self, text: str) -> float:
        """估算一段文本的 TTS 时长作为闸门兜底时限（中文约 4.5 字/秒，下限 8s）。

        仅用于非阻塞播放的保守释放/兜底。

        ⚠️ 2026-10-20 更正：本函数注释原写「真实播完时间无法从 ubus 获知（无完成回调）」
        **已被推翻** —— 设备**有**完成回调：`ubus listen` 收
        `play_status_broadcast_event`（`{"status":1}` 开始 / `{"status":0}` 播完）。
        详见 `docs/devices/lx06/ubus-events-play-status.md`。

        注意该回调的三条边界（并发不可用 / `status:0` 也可能是失败 /
        HTTP 源不可达时只发 `status:1`）⇒ 本估算应**保留为超时兜底上限**，
        而非被完全替换。
        """
        n = len((text or "").strip())
        return max(8.0, min(120.0, n / 4.5 + 6.0))

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

    def _gate_kws(self, kws, reason: str, max_seconds: float = 60.0):
        """开 KWS 播放闸门（失败不阻断播报）。"""
        if not kws:
            return None
        try:
            return kws.gate_on(reason=reason, max_seconds=max_seconds)
        except Exception as e:
            logger.warning(f"[APIServer] KWS gate_on failed ({reason}): {e}")
            return None

    def _ungate_kws(self, kws, token):
        """关 KWS 播放闸门（失败不抛）。"""
        if not kws:
            return
        try:
            kws.gate_off(token)
        except Exception as e:
            logger.warning(f"[APIServer] KWS gate_off failed: {e}")

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
        self.app.router.add_post("/api/inject/stream", self.inject_relay.handle_upstream)  # PC 上行 PCM
        self.app.router.add_get("/api/inject/relay", self.inject_relay.handle_health)  # 中继观测
        self.app.router.add_get("/api/inject/text", self.inject_relay.handle_ws_text)  # 文本回传 WS
        self.app.router.add_get("/api/health", self.handle_health)
        # TTS endpoints
        self.app.router.add_post("/api/tts/doubao", self.handle_tts_doubao)
        self.app.router.add_get("/api/tts/doubao_voices", self.handle_tts_voices)

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
        if self.inject_enabled:
            # 注入流式中继：下游 TCP（hook 拉流）端口 = HTTP 端口 + 1
            await self.inject_relay.start(self.host, self.port + 1)
            logger.info(f"[APIServer] inject 模块已启用（中继 :{self.port + 1}）")
        else:
            # ticket 07：关闭时不监听 :9093；端点统一 503 code=inject_disabled
            # （GET /api/inject 与 /api/health 仍报告 enabled=false，供运维观测）
            logger.info(
                "[APIServer] inject 模块未启用（inject.enabled=false）："
                "不启动中继端口，注入控制/数据面端点返回 503"
            )

    async def stop(self):
        """Stop the HTTP server"""
        self._cancel_inject_expiry()
        await self.inject_relay.stop()
        if self.runner:
            await self.runner.cleanup()
            logger.info("[APIServer] HTTP server stopped")

    # ============ Handlers ============

    def _caller_tag(self, request: web.Request) -> str:
        """返回调用方标识（用于审计“是谁让音箱说话”）。incident §5 建议 7。

        优先取反向代理链头（X-Forwarded-For / X-Real-IP），否则 peer 地址。
        """
        for h in ("X-Forwarded-For", "X-Real-IP"):
            v = request.headers.get(h)
            if v:
                return v.split(",")[0].strip()
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if peer:
            return f"{peer[0]}:{peer[1]}" if len(peer) >= 2 else str(peer)
        return "unknown"

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

            # Run in background to not block the response
            # incident-kws-self-trigger-loop.md §5: 外部播报（HA/企微/DDNS）经此端点发声，
            # 必须开 KWS 播放闸门，否则就是潜在的自触发源（本次事故的真正入口）。
            # 建议 7：记录调用方，回答“是谁让音箱说话的”。
            logger.info(
                f"[APIServer] /api/play/text caller={self._caller_tag(request)} "
                f"blocking={blocking} text={self._redact_text(text)!r}"
            )
            kws = get_kws()
            if blocking:
                gate_seconds = self._mic_gate_seconds(timeout)
                # 先取得 mic_gate 串行锁，再开 KWS 闸门：排队期间 timer 不空转。
                async with speaker.mic_gate(
                    "api/play/text blocking",
                    max_seconds=gate_seconds,
                ):
                    token = self._gate_kws(
                        kws, "api/play/text blocking", max_seconds=gate_seconds
                    )
                    try:
                        result = await speaker.play(
                            text=text, blocking=True, timeout=timeout
                        )
                    finally:
                        self._ungate_kws(kws, token)
                return web.json_response({"success": result})
            else:
                # 非阻塞：播放命令立即返回，用事件适配器等真正播完（超时兜底）。
                estimate = self._estimate_gate_seconds(text)
                budget = estimate + 5.0

                async def _play_and_ungate():
                    async with speaker.mic_gate(
                        "api/play/text background",
                        max_seconds=budget,
                    ):
                        token = self._gate_kws(
                            kws, "api/play/text background", max_seconds=budget
                        )
                        try:
                            await speaker.play(
                                text=text, blocking=False, timeout=timeout
                            )
                            # 用播放完成事件/context 回读等真正结束；超时由适配器兜底。
                            await get_playback_events().wait_for_finish(
                                budget * 1000.0
                            )
                        finally:
                            self._ungate_kws(kws, token)

                asyncio.create_task(_play_and_ungate())
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

            # incident §5: 任何外部播报都需 KWS 闸门（同 /api/play/text）
            # 建议 7：记录调用方
            logger.info(
                f"[APIServer] /api/play/url caller={self._caller_tag(request)} "
                f"blocking={blocking} url={self._redact_text(url, 60)!r}"
            )
            kws = get_kws()

            if blocking:
                gate_seconds = self._mic_gate_seconds(timeout)
                async with speaker.mic_gate(
                    "api/play/url blocking",
                    max_seconds=gate_seconds,
                ):
                    token = self._gate_kws(
                        kws, "api/play/url blocking", max_seconds=gate_seconds
                    )
                    try:
                        result = await speaker.play(
                            url=url, blocking=True, timeout=timeout
                        )
                    finally:
                        self._ungate_kws(kws, token)
                return web.json_response({"success": result})
            else:
                budget = 125.0

                async def _play_url_and_ungate():
                    # 固件闸门覆盖到真正播完（事件适配器 + 超时兜底）。
                    async with speaker.mic_gate(
                        "api/play/url background", max_seconds=budget
                    ):
                        token = self._gate_kws(
                            kws, "api/play/url background", max_seconds=budget
                        )
                        try:
                            await speaker.play(
                                url=url, blocking=False, timeout=timeout
                            )
                            await get_playback_events().wait_for_finish(
                                120_000.0
                            )
                        finally:
                            self._ungate_kws(kws, token)

                asyncio.create_task(_play_url_and_ungate())
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
                # incident §5: 文件播报也走音箱功放，同样需 KWS 闸门
                kws_local = get_kws()
                try:
                    async with speaker.mic_gate("api/play/file", max_seconds=305.0):
                        token = self._gate_kws(
                            kws_local, "api/play/file", max_seconds=305.0
                        )
                        try:
                            success = await speaker.play_server_file(
                                temp_path,
                                blocking=True,
                                sample_rate=sample_rate,
                            )
                        finally:
                            self._ungate_kws(kws_local, token)
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
                "stats": dict(self.inject_relay.stats),
            },
        })

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        GET /api/health
        Health check endpoint（保留只读、可无鉴权；不暴露凭证/完整文本）

        默认只返回内存态：录音帧龄/策略、播放事件面、注入状态。
        `?probe=1` 时额外采集设备探针（mediaplayer 存活 / 内存 / 麦态），
        总超时 3s；探针失败不影响整体健康响应。
        """
        audio: dict = {}
        try:
            from core.xiaoai import XiaoAI

            audio = XiaoAI.audio_health()
        except Exception as e:  # noqa: BLE001 - 健康接口不得因单段失败整体 500
            logger.debug(f"[APIServer] health audio snapshot failed: {e}")

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
                expected_token=configured_token(),
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
                    return {
                        "mediaplayer_alive": await speaker.mediaplayer_alive(),
                        "memory_free": await speaker.memory_free(),
                        "mic": await speaker.get_mic(),
                    }

                try:
                    probe = await asyncio.wait_for(_collect_probe(), timeout=3.0)
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
