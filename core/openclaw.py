"""OpenClaw integration manager for xiaoai.

Configuration:

    1. config.py:
        APP_CONFIG = {
            "openclaw": {
                "enabled": True,
                "url": "ws://localhost:4399",
                "token": "your_token",
                "session_key": "agent:main:open-xiaoai-bridge",
                "identity_path": "~/.openclaw/identity/device.json",
                "ack_timeout": 30,  # Seconds to wait for accepted ack
                "response_timeout": 120,  # Seconds to wait for agent response
            }
        }

    2. Environment variables:
        export OPENCLAW_ENABLE=1  # 兼容旧值 OPENCLAW_ENABLED

Usage:
    from core.openclaw import OpenClawManager
    await OpenClawManager.send("Hello OpenClaw")              # send only
    await OpenClawManager.send_and_play_reply("Hello OpenClaw")     # send + TTS playback

公共骨架（等待超时 / TTS 委托 / 配置装载模板 / 埋点）在
core.backend_base.BackendManagerBase；本模块保留 WS 长连接生命周期
（Ed25519 设备身份、接收循环、心跳监控、指数退避重连、事件处理）与
ack/run-id 映射的传输核。
"""

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from typing import Optional

import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.backend_base import BackendManagerBase
from core.utils.base import get_env
from core.utils.config import ConfigManager  # noqa: F401  兼容面：既有测试 patch core.openclaw.ConfigManager
from core.utils import turn_trace
from core.utils.logger import logger


class OpenClawManager(BackendManagerBase):
    """Manager for OpenClaw connection and messaging."""

    CONFIG_PREFIX = "openclaw"
    # 兼容 OPENCLAW_ENABLE (新) 和 OPENCLAW_ENABLED (旧)
    ENABLE_ENV_VARS = ("OPENCLAW_ENABLE", "OPENCLAW_ENABLED")
    LOG_MODULE = "OpenClaw"

    _min_protocol = 3
    _max_protocol = 4

    # Connection
    _websocket = None
    _connected = False
    _receiver_task = None
    _pending: dict[str, asyncio.Future] = {}
    # B-28 票 02/B5：ack 在途的 idempotencyKey 集合——receiver 对「runId 尚未
    # 记账但有 ack 在途」的 run 事件改挂起等定账，不再静默丢弃。
    _pending_idems: set[str] = set()
    _connect_nonce_future: asyncio.Future | None = None

    # Heartbeat (using OpenClaw tick events + WebSocket built-in ping/pong)
    _heartbeat_task = None
    _heartbeat_interval = 60  # seconds (how often to check connection health)
    _last_tick_time = 0  # last time we received any message/event from server
    _tick_timeout = 120  # seconds (max silence before considering connection dead)

    # Auto-reconnect
    _reconnect_task = None
    _reconnect_enabled = True
    _reconnect_delay = 1  # initial delay in seconds
    _reconnect_max_delay = 60  # max delay in seconds
    _reconnect_attempts = 0
    _should_reconnect = False  # flag to indicate intentional disconnect vs unexpected

    # Config（连接与 TTS 之外的字段归基类；agent_tts_speakers 按 agentId 覆盖）
    DEFAULT_SESSION_KEY = "agent:main:open-xiaoai-bridge"
    _ack_timeout = 60  # seconds to wait for request accepted response
    _agent_tts_speakers: dict[str, str] = {}  # Per-agent speaker overrides
    _url = None
    _token = None
    _identity_path = os.path.expanduser("~/.openclaw/identity/device.json")
    _spki_ed25519_prefix = bytes.fromhex("302a300506032b6570032100")

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # _agent_tts_speakers 同样是每子类独立的记账表（键为 agentId）。
        cls._agent_tts_speakers = {}
        cls._pending = {}
        cls._pending_idems = set()

    @classmethod
    def _apply_config(cls, config: dict):
        """装载 OpenClaw 连接配置；url/token/session_key 变更时触发重连。"""
        previous_url = cls._url
        previous_token = cls._token
        previous_session_key = cls._session_key

        cls._url = config.get("url", "ws://localhost:4399")
        cls._token = config.get("token", "")
        cls._session_key = config.get("session_key", cls.DEFAULT_SESSION_KEY)
        cls._identity_path = cls._resolve_identity_path(config.get("identity_path"))
        raw_agent_speakers = config.get("agent_tts_speakers", {})
        cls._agent_tts_speakers = (
            {
                str(key): str(value)
                for key, value in raw_agent_speakers.items()
                if key and value
            }
            if isinstance(raw_agent_speakers, dict)
            else {}
        )
        cls._ack_timeout = config.get("ack_timeout", 30)

        should_reconnect = cls._connected and (
            previous_url != cls._url
            or previous_token != cls._token
            or previous_session_key != cls._session_key
        )
        if should_reconnect:
            logger.info("[OpenClaw] Runtime config changed, reconnecting with new settings")
            try:
                from core.ref import get_app

                app = get_app()
                if app and app.loop and app.loop.is_running():
                    async def reconnect():
                        await cls.close()
                        await cls.connect()

                    asyncio.run_coroutine_threadsafe(reconnect(), app.loop)
            except Exception as exc:
                logger.warning(f"[OpenClaw] Failed to reconnect after config reload: {exc}")

    @classmethod
    def _log_enabled(cls):
        logger.info(f"[OpenClaw] Enabled, will connect to {cls._url}")
        logger.info(f"[OpenClaw] Device identity path: {cls._identity_path}")

    @classmethod
    def get_tts_speaker_for_session_key(cls, session_key: str | None = None) -> str | None:
        """Resolve TTS speaker for the agentId in a session key."""
        if not cls._initialized:
            cls.initialize_from_config()

        target_session_key = session_key or cls._session_key
        if target_session_key:
            parts = target_session_key.split(":")
            if len(parts) >= 2:
                agent_id = parts[1]
                agent_match = cls._agent_tts_speakers.get(agent_id)
                if agent_match:
                    logger.debug(
                        f"[OpenClaw] Resolved TTS speaker: "
                        f"agent_id={agent_id}, speaker={agent_match}"
                    )
                    return agent_match

        return cls._tts_speaker

    @classmethod
    def _resolve_identity_path(cls, configured_path: Optional[str]) -> str:
        env_path = get_env("OPENCLAW_DEVICE_IDENTITY_PATH")
        chosen = env_path if env_path else configured_path
        if not chosen:
            return os.path.expanduser("~/.openclaw/identity/device.json")
        return os.path.expanduser(chosen)

    @classmethod
    def _load_or_create_device_identity(cls) -> dict:
        path = cls._identity_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                if (
                    isinstance(parsed, dict)
                    and parsed.get("version") == 1
                    and isinstance(parsed.get("deviceId"), str)
                    and isinstance(parsed.get("publicKeyPem"), str)
                    and isinstance(parsed.get("privateKeyPem"), str)
                ):
                    return parsed
        except Exception as e:
            logger.warning(f"[OpenClaw] Failed loading device identity, regenerating: {e}")

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        spki_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        raw = (
            spki_der[len(cls._spki_ed25519_prefix) :]
            if spki_der.startswith(cls._spki_ed25519_prefix)
            and len(spki_der) == len(cls._spki_ed25519_prefix) + 32
            else spki_der
        )
        device_id = hashlib.sha256(raw).hexdigest()
        stored = {
            "version": 1,
            "deviceId": device_id,
            "publicKeyPem": public_pem,
            "privateKeyPem": private_pem,
            "createdAtMs": int(time.time() * 1000),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return stored

    @classmethod
    def _base64url_encode(cls, raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def _normalize_metadata_for_auth(cls, value: Optional[str]) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip().lower()

    @classmethod
    def _build_device_signature(cls, *, token: str, nonce: str, scopes: list[str], client: dict) -> dict:
        identity = cls._load_or_create_device_identity()
        private_key = serialization.load_pem_private_key(
            identity["privateKeyPem"].encode("utf-8"),
            password=None,
        )
        public_key = serialization.load_pem_public_key(identity["publicKeyPem"].encode("utf-8"))
        spki_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_key_raw = (
            spki_der[len(cls._spki_ed25519_prefix) :]
            if spki_der.startswith(cls._spki_ed25519_prefix)
            and len(spki_der) == len(cls._spki_ed25519_prefix) + 32
            else spki_der
        )
        signed_at_ms = int(time.time() * 1000)
        platform = cls._normalize_metadata_for_auth(client.get("platform"))
        device_family = cls._normalize_metadata_for_auth(client.get("deviceFamily"))
        payload = "|".join(
            [
                "v3",
                identity["deviceId"],
                client.get("id") or "",
                client.get("mode") or "",
                "operator",
                ",".join(scopes),
                str(signed_at_ms),
                token or "",
                nonce,
                platform,
                device_family,
            ]
        )
        signature = private_key.sign(payload.encode("utf-8"))
        return {
            "id": identity["deviceId"],
            "publicKey": cls._base64url_encode(public_key_raw),
            "signature": cls._base64url_encode(signature),
            "signedAt": signed_at_ms,
            "nonce": nonce,
        }

    @classmethod
    def _build_connect_params(
        cls,
        client_meta: dict,
        scopes: list[str],
        token: str | None,
        device_payload: dict | None = None,
    ) -> dict:
        connect_params = {
            "minProtocol": cls._min_protocol,
            "maxProtocol": cls._max_protocol,
            "client": client_meta,
            "locale": "zh-CN",
            "userAgent": "open-xiaoai-bridge/1.0.0",
            "role": "operator",
            "scopes": scopes,
            "caps": [],
            "auth": {"token": token if token is not None else ""},
        }
        if device_payload:
            connect_params["device"] = device_payload
        return connect_params

    @classmethod
    async def connect(cls) -> bool:
        """Connect to OpenClaw gateway."""
        if not cls._initialized:
            cls.initialize_from_config()

        if not cls._enabled:
            return False

        if cls._connected and cls._websocket:
            return True

        try:
            logger.info(f"[OpenClaw] Connecting to {cls._url}...")
            cls._websocket = await websockets.connect(cls._url)
            logger.info(f"[OpenClaw] WebSocket connected, sending handshake...")
            cls._connect_nonce_future = asyncio.get_running_loop().create_future()
            cls._receiver_task = asyncio.create_task(cls._receiver())

            nonce: str | None = None
            try:
                nonce = await asyncio.wait_for(cls._connect_nonce_future, timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[OpenClaw] connect.challenge nonce wait timed out; continuing without device auth")

            client_meta = {
                "id": "gateway-client",
                "displayName": "Open-Xiaoai Bridge",
                "version": "1.0.0",
                "platform": "python",
                "mode": "backend",
                "instanceId": f"xiaoai-{uuid.uuid4().hex[:8]}",
            }

            scopes = ["operator.read", "operator.write"]
            device_payload = (
                cls._build_device_signature(
                    token=cls._token or "",
                    nonce=nonce,
                    scopes=scopes,
                    client=client_meta,
                )
                if nonce
                else None
            )

            connect_params = cls._build_connect_params(
                client_meta=client_meta,
                scopes=scopes,
                token=cls._token,
                device_payload=device_payload,
            )

            # Send connect request
            res = await cls._request(
                "connect",
                connect_params,
                timeout=10,
            )

            if res.get("ok"):
                cls._connected = True
                cls._should_reconnect = True
                cls._reconnect_attempts = 0
                cls._connect_nonce_future = None
                cls._last_tick_time = asyncio.get_event_loop().time()
                logger.info(f"[OpenClaw] Connected to {cls._url}")
                # Start heartbeat monitor task
                cls._heartbeat_task = asyncio.create_task(cls._heartbeat())
                return True
            else:
                error = (res.get("error") or {}).get("message") or "connect failed"
                logger.error(f"[OpenClaw] Connection failed: {error}")
                cls._connect_nonce_future = None
                cls._trigger_reconnect()
                return False

        except Exception as e:
            import traceback
            logger.error(f"[OpenClaw] Connection error: {type(e).__name__}: {e}")
            logger.debug(f"[OpenClaw] Connection error traceback: {traceback.format_exc()}")
            cls._connected = False
            cls._websocket = None
            cls._connect_nonce_future = None
            cls._trigger_reconnect()
            return False

    @classmethod
    async def close(cls):
        """Close the connection."""
        cls._should_reconnect = False
        cls._connected = False
        cls._connect_nonce_future = None
        # Cancel reconnect task
        if cls._reconnect_task:
            cls._reconnect_task.cancel()
            cls._reconnect_task = None
        # Cancel heartbeat task
        if cls._heartbeat_task:
            cls._heartbeat_task.cancel()
            cls._heartbeat_task = None
        if cls._receiver_task:
            cls._receiver_task.cancel()
            cls._receiver_task = None
        if cls._websocket:
            await cls._websocket.close()
            cls._websocket = None

    @classmethod
    def is_connected(cls) -> bool:
        """Check if connected to OpenClaw."""
        return cls._connected and cls._websocket is not None

    # send / send_and_play_reply 直接继承基类：OpenClaw 的回复经 WS 事件由
    # 接收循环 resolve 记账 Future，基类的 _wait_response/_discard_response
    # 对该模型同样成立（fire-and-forget 挂后台等待收尾，事件路径照样派发）。

    # -- internal helpers --------------------------------------------------

    @classmethod
    async def _send_and_track(cls, text: str) -> str | None:
        """Send message, wait for ack, set up response tracking.

        Returns the run_id on success, or None on failure.
        """
        if not cls._initialized:
            cls.initialize_from_config()

        if not cls._enabled:
            logger.warning("[OpenClaw] send called but backend is disabled")
            return None

        if not cls._connected:
            logger.info("[OpenClaw] send called but not connected, trying to connect...")
            if not await cls.connect():
                return None

        try:
            idem = str(uuid.uuid4())
            logger.user_speech(text, module=f"OpenClaw({cls._session_key})")
            # TurnTrace：WS 后端同样接入延迟埋点（无本地完成任务的模板路径，
            # 起点在此手工挂；终点在 _on_response_finalized / 失败分支）。
            turn_trace.mark_llm_start()
            # 用量记账维度（P2 ticket 03）：网关不暴露模型名，model 留空（面板显示 -）。
            turn_trace.note_context(
                backend=cls.LOG_MODULE,
                model=cls.usage_model_label(),
                session_key=cls._session_key,
            )
            # 一次 agent 请求 = 一次记账请求（无本地工具回环）
            turn_trace.note_llm_request()

            cls._open_run(idem, cls.get_tts_speaker_for_session_key(cls._session_key))
            cls._pending_idems.add(idem)
            logger.debug(f"[OpenClaw] Tracking idem={idem}")

            request_params = {
                "message": text,
                "sessionKey": cls._session_key,
                "deliver": False,
                "idempotencyKey": idem,
            }

            req_id, ack_future = await cls._send_request_with_future("agent", request_params)
            logger.debug(f"[OpenClaw] Agent request sent, req_id={req_id}, idem={idem}")

            try:
                res = await asyncio.wait_for(ack_future, timeout=cls._ack_timeout)
                ok = res.get("ok") if isinstance(res, dict) else False
                payload = res.get("payload") if isinstance(res, dict) else {}
                run_id = payload.get("runId") if isinstance(payload, dict) else None
                status = payload.get("status") if isinstance(payload, dict) else None

                if not ok:
                    err = (res.get("error") or {}).get("message") if isinstance(res, dict) else "agent request rejected"
                    cls.last_error = err or "agent request rejected"
                    logger.error(f"[OpenClaw] Agent request rejected: req_id={req_id}, idem={idem}, error={cls.last_error}")
                    cls._drop_tracked_run(idem)
                    turn_trace.note_llm_error()
                    turn_trace.mark_llm_end()
                    return None

                final_run_id = run_id or idem
                logger.debug(
                    f"[OpenClaw] Agent request accepted: req_id={req_id}, runId={final_run_id}, status={status}"
                )
                if final_run_id != idem and idem in cls._response_events:
                    cls._response_events[final_run_id] = cls._response_events.pop(idem)
                    cls._response_texts[final_run_id] = cls._response_texts.pop(idem, "")
                    cls._response_tts_speakers[final_run_id] = cls._response_tts_speakers.pop(
                        idem, None
                    )

                return final_run_id
            except asyncio.TimeoutError:
                logger.warning(f"[OpenClaw] Agent ack timeout: req_id={req_id}, idem={idem}, timeout={cls._ack_timeout}s")
                cls._drop_tracked_run(idem)
                turn_trace.note_llm_error()
                turn_trace.mark_llm_end()
                return None
            finally:
                cls._pending.pop(req_id, None)
        except Exception as e:
            import traceback
            logger.error(f"[OpenClaw] Failed to send message: {type(e).__name__}: {e}")
            logger.debug(f"[OpenClaw] Send message traceback: {traceback.format_exc()}")
            turn_trace.note_llm_error()
            turn_trace.mark_llm_end()
            return None
        finally:
            # B-28 票 02/B5：ack 定账（成/败/超时）后 idem 即退出在途集
            cls._pending_idems.discard(idem)

    @classmethod
    def _drop_tracked_run(cls, run_id: str):
        """清掉一次 run 的记账三元组，并安全唤醒其等待者。"""
        waiter = cls._response_events.pop(run_id, None)
        cls._response_texts.pop(run_id, None)
        cls._response_tts_speakers.pop(run_id, None)
        if waiter and not waiter.done():
            waiter.get_loop().call_soon_threadsafe(waiter.set_result, None)

    @classmethod
    def _on_response_finalized(cls, run_id: str):
        """TurnTrace：WS 后端的 LLM 段在响应定稿（守卫内单次）收口。"""
        turn_trace.note_reply_chars(len(cls._response_texts.get(run_id, "")))
        turn_trace.mark_llm_end()

    @classmethod
    async def _request(cls, method: str, params=None, timeout: float = 30):
        """Send a request and wait for response."""
        req_id, fut = await cls._send_request_with_future(method, params)
        logger.debug(f"[OpenClaw] _request waiting for response: req_id={req_id}, method={method}")
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            payload = result.get("payload") if isinstance(result, dict) else {}
            status = payload.get("status") if isinstance(payload, dict) else None
            logger.debug(
                f"[OpenClaw] _request received response: req_id={req_id}, ok={result.get('ok') if isinstance(result, dict) else None}, status={status}"
            )
            return result
        except asyncio.TimeoutError:
            logger.error(f"[OpenClaw] Request timeout: method={method}, req_id={req_id}, timeout={timeout}s")
            raise
        finally:
            cls._pending.pop(req_id, None)
            logger.debug(f"[OpenClaw] _request cleaned up: req_id={req_id}")

    @classmethod
    async def _send_request_with_future(cls, method: str, params=None) -> tuple[str, asyncio.Future]:
        """Send request frame and return (request_id, response_future)."""
        if not cls._websocket:
            raise RuntimeError("OpenClaw websocket is not connected")

        req_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        cls._pending[req_id] = fut

        request_payload = {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        logger.debug(f"[OpenClaw] _request sending: req_id={req_id}, method={method}")
        await cls._websocket.send(json.dumps(request_payload))
        return req_id, fut

    @classmethod
    async def _receiver(cls):
        """Background task to receive responses and events."""
        try:
            async for message in cls._websocket:
                # Update last activity time for any message (including WebSocket ping/pong frames)
                cls._last_tick_time = asyncio.get_event_loop().time()

                if isinstance(message, bytes):
                    # WebSocket ping/pong frames are handled automatically by websockets library
                    continue

                if not isinstance(message, str):
                    continue

                try:
                    data = json.loads(message)
                    msg_type = data.get("type")

                    # Handle event messages (including tick events from OpenClaw)
                    if msg_type == "event":
                        event_name = data.get("event", "")
                        if event_name == "connect.challenge":
                            nonce = ((data.get("payload") or {}).get("nonce") or "").strip()
                            if nonce and cls._connect_nonce_future and not cls._connect_nonce_future.done():
                                cls._connect_nonce_future.set_result(nonce)
                                logger.debug("[OpenClaw] connect.challenge nonce received")
                        elif event_name == "tick":
                            logger.debug("[OpenClaw] Tick event received")
                        # Handle agent response events for TTS playback
                        elif event_name in ("run.completed", "run.output", "run.text", "agent"):
                            cls._receiver_dispatch_agent_event(data)
                        else:
                            logger.debug(f"[OpenClaw] Other event received: {event_name}, data: {data}")
                        # Any event counts as server activity
                        continue

                    if msg_type != "res":
                        logger.debug(f"[OpenClaw] Non-res message type: {msg_type}, data: {data}")
                        continue

                    req_id = data.get("id")
                    if not req_id:
                        continue

                    future = cls._pending.get(req_id)
                    logger.debug(
                        f"[OpenClaw] _receiver processing res: req_id={req_id}, has_future={future is not None}, future_done={future.done() if future else None}"
                    )
                    if future and not future.done():
                        logger.debug(f"[OpenClaw] _receiver setting result for req_id={req_id}")
                        # Complete the Future on its owner loop to avoid cross-loop wakeup delays.
                        fut_loop = future.get_loop()
                        fut_loop.call_soon_threadsafe(future.set_result, data)
                        # Note: OpenClaw may send a second res for agent method (completed status)
                        # We keep the entry in _pending to handle potential second response
                    elif future and future.done():
                        # Second res for agent method — clean up now that both responses arrived
                        cls._pending.pop(req_id, None)
                        payload = data.get("payload") if isinstance(data, dict) else {}
                        status = payload.get("status") if isinstance(payload, dict) else None
                        summary = payload.get("summary") if isinstance(payload, dict) else None
                        logger.debug(
                            f"[OpenClaw] _receiver cleaned up second res for req_id={req_id}, status={status}, summary={summary}"
                        )
                except json.JSONDecodeError:
                    logger.warning(f"[OpenClaw] Failed to decode message: {message[:200]}")
        except asyncio.CancelledError:
            logger.debug("[OpenClaw] Receiver task cancelled")
            raise
        except Exception as e:
            logger.warning(f"[OpenClaw] Receiver error: {type(e).__name__}: {e}")
        finally:
            # Clean up all pending futures to prevent leak on disconnect
            for req_id, fut in list(cls._pending.items()):
                if not fut.done():
                    fut.cancel()
            cls._pending.clear()
            cls._pending_idems.clear()  # 断连后 ack 永不再来，在途集一并清空
            cls._connected = False
            cls._trigger_reconnect()

    @classmethod
    def _trigger_reconnect(cls):
        """Trigger reconnection if enabled and not manually closed."""
        if not cls._should_reconnect or not cls._reconnect_enabled or not cls._enabled:
            return
        if cls._reconnect_task is None or cls._reconnect_task.done():
            cls._reconnect_task = asyncio.create_task(cls._reconnect())

    @classmethod
    async def _reconnect(cls):
        """Background task to reconnect with exponential backoff."""
        while cls._should_reconnect and cls._enabled and not cls._connected:
            cls._reconnect_attempts += 1
            delay = min(
                cls._reconnect_delay * (2 ** (cls._reconnect_attempts - 1)),
                cls._reconnect_max_delay
            )
            logger.info(f"[OpenClaw] Reconnecting in {delay}s (attempt #{cls._reconnect_attempts})...")
            await asyncio.sleep(delay)

            if cls._connected:
                break

            try:
                success = await cls.connect()
                if success:
                    logger.info(f"[OpenClaw] Reconnected successfully after {cls._reconnect_attempts} attempts")
                    break
            except Exception as e:
                logger.warning(f"[OpenClaw] Reconnect attempt failed: {e}")

    @classmethod
    async def _heartbeat(cls):
        """Background task to monitor connection health.

        Uses WebSocket built-in ping/pong (handled automatically by websockets library)
        and monitors server activity (tick events or any messages).
        """
        try:
            while cls._connected and cls._websocket:
                await asyncio.sleep(cls._heartbeat_interval)

                if not cls._connected or not cls._websocket:
                    break

                # Check if we've heard from the server recently
                current_time = asyncio.get_event_loop().time()
                silence_duration = current_time - cls._last_tick_time

                if silence_duration > cls._tick_timeout:
                    logger.warning(
                        f"[OpenClaw] Connection appears dead (no activity for {silence_duration:.0f}s), "
                        f"triggering reconnect"
                    )
                    cls._connected = False
                    cls._trigger_reconnect()
                    break

                logger.debug(f"[OpenClaw] Connection healthy (last activity {silence_duration:.0f}s ago)")
        except asyncio.CancelledError:
            logger.debug("[OpenClaw] Heartbeat task cancelled")
            raise
        except Exception as e:
            logger.error(f"[OpenClaw] Heartbeat error: {e}")
            cls._connected = False
            cls._trigger_reconnect()

    @classmethod
    def _receiver_dispatch_agent_event(cls, data: dict):
        """receiver 侧 run 事件分派：runId 已入账直通处理；ack 在途走早到通道。

        B-28 票 02/B5：早到 = runId 尚未记账（记账还挂在 idem 键）但确有 ack
        在途——挂起等定账，不再静默丢弃。只分派不等待，不阻塞 res 帧处理。
        """
        run_id = (data.get("payload") or {}).get("runId")
        if run_id and run_id in cls._response_events:
            logger.debug(f"[OpenClaw] Agent event received: {data.get('event')}, runId={run_id}")
            # Do not block receiver loop on event handling; keep processing res frames promptly.
            asyncio.create_task(cls._handle_agent_event(data))
        elif run_id and cls._pending_idems:
            asyncio.create_task(cls._handle_early_agent_event(data))

    @classmethod
    async def _handle_early_agent_event(cls, event_data: dict):
        """ack 在途期早到的 run 事件（B-28 票 02/B5）：等 ack 定账后重投。

        网关先推 run 事件后回 ack 时，runId 此刻还未记账（记账挂在 idem 键）；
        旧实现按「runId 不在账」直接丢弃整条事件 = 丢文本。此处最多等
        ack_timeout + 2s，等不到（ack 失败/超时/事件属于未知 run）再丢弃并留
        WARNING，行为严格优于静默丢弃。
        """
        run_id = (event_data.get("payload") or {}).get("runId")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + cls._ack_timeout + 2.0
        while loop.time() < deadline:
            if run_id in cls._response_events or not cls._pending_idems:
                break
            await asyncio.sleep(0.05)
        if run_id in cls._response_events:
            await cls._handle_agent_event(event_data)
        else:
            logger.warning(
                f"[OpenClaw] early agent event dropped (run never booked): "
                f"event={event_data.get('event')}, runId={run_id}"
            )

    @classmethod
    async def _handle_agent_event(cls, event_data: dict):
        """Handle agent events to extract response text for TTS playback."""
        try:
            event_name = event_data.get("event", "")
            payload = event_data.get("payload", {})
            run_id = payload.get("runId")

            logger.debug(f"[OpenClaw] Event '{event_name}' runId={run_id}")

            if event_name == "run.completed":
                # Final response
                output = payload.get("output", {})
                response_text = output.get("text", "")

                if run_id and response_text:
                    logger.debug(f"[OpenClaw] run.completed runId={run_id}: {response_text[:80]}")
                    if cls._store_response(run_id, response_text):
                        cls._signal_response_ready(run_id)

            elif event_name == "run.output":
                # Streaming output
                output = payload.get("output", {})
                chunk_text = output.get("text", "")

                if run_id and chunk_text:
                    logger.debug(f"[OpenClaw] Output chunk for {run_id}: {chunk_text[:50]}...")
                    # Accumulate text chunks（孤儿 run 由 _store_response 拒绝）
                    cls._store_response(run_id, chunk_text, append=True)

            elif event_name == "run.text":
                # Text event
                text = payload.get("text", "")

                if run_id and text:
                    logger.debug(f"[OpenClaw] run.text runId={run_id}: {text[:80]}")
                    if cls._store_response(run_id, text):
                        cls._signal_response_ready(run_id)

            elif event_name == "agent":
                # Agent event from OpenClaw (stream: lifecycle or assistant)
                stream = payload.get("stream", "")
                data = payload.get("data", {})

                if stream == "assistant":
                    # Streaming text from assistant
                    text = data.get("text", "")
                    delta = data.get("delta", "")

                    if run_id and (text or delta):
                        logger.debug(
                            f"[OpenClaw] Agent assistant stream for {run_id}: text len={len(text or '')}, delta_len={len(delta) if delta else 0}"
                        )
                        # text 是完整快照；新版协议的后续事件可能只携带 delta。
                        # 同时存在二者时使用快照，避免重复拼接首个增量。
                        # （孤儿 run 由 _store_response 拒绝——B-28 票 02/B1）
                        if text:
                            cls._store_response(run_id, text)
                        elif delta:
                            cls._store_response(run_id, delta, append=True)
                        # 等待生命周期结束后再提交完整回复。

                elif stream == "lifecycle":
                    phase = data.get("phase", "")

                    if phase == "end":
                        # Agent run completed
                        response_text = cls._response_texts.get(run_id, "")
                        if run_id and response_text and run_id in cls._response_events:
                            logger.ai_response(response_text, module=f"OpenClaw({cls._session_key})")
                        cls._signal_response_ready(run_id)

        except Exception as e:
            logger.error(f"[OpenClaw] Error handling agent event: {e}")
            logger.debug(f"[OpenClaw] Event data: {event_data}")

    # _wait_and_play_response 覆写已删除（B-28 票 02/W2）：原覆写与基类逐行等价
    # （仅多两行日志）；异常守卫已收编进基类。


# No auto-initialization - call OpenClawManager.initialize_from_config() explicitly
