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
from core.ref import get_speaker, get_xiaoai, get_kws
from core.wakeup_session import EventManager
from core.services.admin_api import AdminAPI
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
    """

    def __init__(self):
        self.chunks: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.active: bool = False
        self.downstream_writer: asyncio.StreamWriter | None = None
        self._downstream_task: asyncio.Task | None = None
        self.stats = {"up_bytes": 0, "down_bytes": 0, "sessions": 0}

    def open_session(self) -> None:
        """开窗：允许上行数据入队。"""
        self.active = True
        self.stats["sessions"] += 1
        logger.info("[InjectRelay] 会话开启（上游可推流）")

    def close_session(self, reason: str = "closed") -> None:
        """关窗：丢弃后续上行 + 断开下游 hook（hook 会自动重连等下一窗）。"""
        if not self.active and self.downstream_writer is None:
            return
        self.active = False
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
        """
        caller = self._caller(request) if hasattr(self, "_caller") else "?"
        if not self.active:
            return web.json_response(
                {"success": False, "error": "injection window not open"},
                status=409,
            )
        logger.info(f"[InjectRelay] 上行开始 caller={caller}")
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
        self._downstream_task = asyncio.create_task(self.downstream_server(host, port))

    async def stop(self) -> None:
        self.close_session("relay stop")
        if self._downstream_task:
            self._downstream_task.cancel()

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "success": True,
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
        # Admin 面板鉴权中间件（只拦 /api/admin/*，业务 API 不受影响）
        self.admin_api = AdminAPI()
        self.app = web.Application(middlewares=[self.admin_api.auth_middleware])
        self.runner = None
        self.site = None
        self.inject_relay = InjectRelay()   # 注入流式中继（数据面）
        global _relay_singleton
        _relay_singleton = self.inject_relay
        self._setup_routes()
        self.admin_api.register(self.app)

    def _estimate_gate_seconds(self, text: str) -> float:
        """估算一段文本的 TTS 时长作为闸门兜底时限（中文约 4.5 字/秒，下限 8s）。

        仅用于非阻塞播放的保守释放/兜底；真实播完时间无法从 ubus 获知（无完成回调）。
        """
        n = len((text or "").strip())
        return max(8.0, min(120.0, n / 4.5 + 6.0))

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
        # 注入流式中继：下游 TCP（hook 拉流）端口 = HTTP 端口 + 1
        relay_port = self.port + 1
        await self.inject_relay.start(self.host, relay_port)

    async def stop(self):
        """Stop the HTTP server"""
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
                token = self._gate_kws(kws, "api/play/text blocking")
                try:
                    result = await speaker.play(text=text, blocking=True, timeout=timeout)
                finally:
                    self._ungate_kws(kws, token)
                return web.json_response({"success": result})
            else:
                # 非阻塞：无法预知播放何时结束，用兜底时分自动恢复。
                token = self._gate_kws(
                    kws, "api/play/text background",
                    max_seconds=self._estimate_gate_seconds(text),
                )

                async def _play_and_ungate():
                    try:
                        await speaker.play(text=text, blocking=False, timeout=timeout)
                    finally:
                        # 非阻塞播放在音箱内部异步进行，这里给一个保守的释放延时；
                        # 真正的兜底由闸门 timer 保证（调用方异常也不会永久失效）。
                        await asyncio.sleep(self._estimate_gate_seconds(text))
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
                token = self._gate_kws(kws, "api/play/url blocking")
                try:
                    result = await speaker.play(url=url, blocking=True, timeout=timeout)
                finally:
                    self._ungate_kws(kws, token)
                return web.json_response({"success": result})
            else:
                token = self._gate_kws(kws, "api/play/url background", max_seconds=120.0)

                async def _play_url_and_ungate():
                    try:
                        await speaker.play(url=url, blocking=False, timeout=timeout)
                    finally:
                        await asyncio.sleep(120.0)
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
                token = self._gate_kws(kws_local, "api/play/file", max_seconds=300.0)
                try:
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
                    self._ungate_kws(kws_local, token)
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

            # T7.6 修复：set_mic(True) 的 ubus event:7 不会清除设备端静音标志
            # (/tmp/mipns/mute，由 set_mic(False) 的 ubus event:8 在宿主侧创建)，
            # 导致宿主机仍静音、KWS 收不到语音、恢复后④⑤ 无响应。
            # 显式删掉该宿主侧标志，与「停止聆听」时对称地真正恢复采集。
            try:
                await speaker.run_shell("rm -f /tmp/mipns/mute")
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

    async def handle_inject(self, request: web.Request) -> web.Response:
        """
        POST /api/inject
        PC 侧 ASR 注入窗口控制（firmware-asr-injection-feasibility.md §3.4/§3.5）。

        开窗 = 开注入闸门（机制 A）+ P2 静默唤醒引擎（pnshelper event_notify）。
        唤醒后引擎 enable asr=1，云端 ASR 才会出流；闸门保证识别结果不会
        被当成用户指令交给 Agent/对话后端。

        请求体：
            {"on": true,  "duration": 30}   # 开窗（duration 可选，默认/上限见 TTL）
            {"on": false}                    # 关窗（PC finally 必须调用）

        Response:
            {"success": true, "gate": "open"/"closed", "remaining": <秒>}
        """
        try:
            data = await request.json() if request.can_read_body else {}
            caller = self._caller(request)

            if data.get("on"):
                duration = data.get("duration")
                if duration is not None:
                    try:
                        duration = min(max(float(duration), 1.0), EventManager.INJECTION_GATE_TTL)
                    except (TypeError, ValueError):
                        duration = None
                ttl = EventManager.open_injection_gate(duration)
                self.inject_relay.open_session()   # 数据面：允许上游推流

                # P2 唤醒路径：复用 xiaoai_asr 模式的静默唤醒，使引擎进入 ASR 出流态。
                speaker = get_speaker()
                woken = None
                if speaker:
                    try:
                        woken = await speaker.wake_up(awake=True, silent=True)
                    except Exception as e:
                        logger.warning(f"[APIServer] inject wake_up failed: {e}")

                logger.info(
                    f"[APIServer] /api/inject 开窗 caller={caller} ttl={ttl:.0f}s wake_up={woken}"
                )
                return web.json_response({
                    "success": True,
                    "gate": "open",
                    "remaining": EventManager.injection_gate_remaining(),
                    "wakeup": woken,
                })

            EventManager.close_injection_gate()
            self.inject_relay.close_session("api close")   # 数据面：断下游 + 丢上行
            logger.info(f"[APIServer] /api/inject 关窗 caller={caller}")
            return web.json_response({"success": True, "gate": "closed", "remaining": 0})

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
        """
        return web.json_response({
            "success": True,
            "gate": "open" if EventManager.injection_gate_active() else "closed",
            "remaining": EventManager.injection_gate_remaining(),
        })

    async def handle_health(self, request: web.Request) -> web.Response:
        """
        GET /api/health
        Health check endpoint
        """
        return web.json_response({
            "success": True,
            "data": {
                "status": "healthy",
                "speaker_ready": get_speaker() is not None
            }
        })

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
