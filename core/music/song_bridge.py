"""旧 `song_request` → 托管队列的迁移桥（B-06，flag 后启用）。

开关：`song_request.managed`（config，默认 false）。关 = 旧内存队列原样工作
（回滚模式）；开 = 搜索/专辑候选逻辑照旧复用，**队列状态改由托管 store 持有**，
播放推进由 `ManagedRuntime` 的 tick 驱动。

铁律：同一时刻只有一条队列所有权——flag 打开后旧 `_state` 不再维护队列语义，
不会出现「两个自动推进」。
"""

from __future__ import annotations

from core.music.backends import RpcBackend
from core.music.model import ManagedError, TrackRef
from core.music.player import ManagedQueuePlayer
from core.music.runtime import ManagedRuntime, get_runtime, set_runtime
from core.music.service import ManagedService
from core.music.store import ManagedStore
from core.tools import song_request as _sr


def managed_enabled() -> bool:
    return bool(_sr._cfg("managed", False))


def target() -> str:
    return str(_sr._cfg("managed_target", "lx06") or "lx06")


def build_runtime() -> ManagedRuntime:
    runtime = get_runtime()
    if runtime is not None:
        return runtime
    store = ManagedStore()
    # B-04：重启只还原元数据并把旧会话置 recovery_pending——绝不自动起播
    restored = store.restore()
    if restored:
        from core.utils.logger import logger

        logger.info(f"[SongBridge] restored {len(restored)} managed session(s) -> recovery_pending")
    service = ManagedService(store=store)
    # 设备通道是 mediaplayer 的 player_play_url（RPC），不是 DLNA SOAP；
    # 它已验证 pause/play（设备播放控制 CLI），所以用 RpcBackend 的能力矩阵。
    resolver = _songloft_resolver if str(_sr._cfg("managed_media_resolver", "kwqq")) == "songloft" else _default_resolver
    player = ManagedQueuePlayer(store, RpcBackend(), resolver)
    runtime = ManagedRuntime(player, service=service)
    set_runtime(runtime)
    return runtime


async def _default_resolver(entry):
    """按 TrackRef 解析当前曲直链（复用 song_request 的两跳逻辑与源白名单）。"""
    import aiohttp

    item = {"id": entry.ref.id, "source": entry.ref.source,
            "extra": entry.ref.extra_dict}
    async with aiohttp.ClientSession() as session:
        url = await _sr._pick_url(session, entry.ref.source, item)
    if not url:
        raise ManagedError("no playable url for the current entry")
    return url


async def play_items(items: list[dict], *, start_index: int = 0, album: bool = False) -> str:
    """用托管会话接管队列：新会话 + 入队 + 起播当前项。"""

    if not items:
        return "没有可播放的曲目"
    runtime = build_runtime()
    service: ManagedService = runtime.service
    store = service.store
    if runtime.session_id:
        try:
            service.command(runtime.session_id, kind="stop", principal="song_request",
                            idem_key=f"stop-{runtime.session_id}")
        except Exception:  # noqa: BLE001 - 旧会话已失效时直接换新
            pass
    snapshot = service.open_session(target=target(), transport="rpc",
                                    player_backend="mediaplayer", principal="song_request")
    sid = snapshot["session_id"]
    for item in items:
        store.append(
            sid,
            TrackRef.make(item.get("source"), item.get("id"), item.get("extra")),
            quality="320k",
            duration_s=_sr._duration_s(item),
        )
    if 0 <= start_index < len(items):
        store.set_current(sid, store.get(sid).entries[start_index].entry_id)
    runtime.bind(sid)
    await runtime.player.start(sid)
    await runtime.start()
    return f"托管队列已接管（{len(items)} 首）"


async def command(kind: str, **payload) -> str:
    runtime = get_runtime()
    if runtime is None or not runtime.session_id:
        return "托管队列还没有内容"
    service: ManagedService = runtime.service
    receipt = service.command(
        runtime.session_id, kind=kind, payload=payload, principal="song_request",
        idem_key=f"{kind}-{runtime.session_id}-{service.store.get(runtime.session_id).epoch}",
        target=target(),
    )
    # 设备动作与状态分离：命令先落状态/回执，再驱动设备；设备确认后 observe。
    try:
        if kind == "pause":
            await runtime.player.pause_backend()
            service.observe(receipt.receipt_id)
        elif kind == "resume":
            # 用户显式恢复：立即放弃会话级 focus（不再等去抖），再驱动设备恢复。
            from core.music import focus_bridge

            await focus_bridge.force_release()
            # 先 resume 再**回读验证**：TTS/唤醒可能已替换媒体上下文，
            # 此时设备 play 是假成功；验证不到 playing 就重起当前 entry。
            outcome = await runtime.player.ensure_resumed(runtime.session_id)
            if outcome not in ("resumed", "restarted"):
                raise ManagedError(f"resume outcome={outcome}")
            service.observe(receipt.receipt_id)
        elif kind in ("next", "prev"):
            await runtime.player.start(runtime.session_id)
            service.observe(receipt.receipt_id)
    except Exception as exc:  # noqa: BLE001 - 设备拒绝要如实回报，不假装成功
        service.observe(receipt.receipt_id, state="failed", detail=str(exc)[:160])
        return f"托管命令未能执行（{kind}）：{exc}"
    if kind == "stop":
        from core.music import focus_bridge

        await focus_bridge.force_release()
        await runtime.stop()
        runtime.bind(None)
    return f"托管命令已受理（{kind}, {receipt.state}）"


DEVICE_COMMANDS = ("play", "pause", "resume", "next", "prev", "stop")


async def apply_command(session_id: str, kind: str, receipt_id: str) -> None:
    """HTTP/App 命令的设备执行侧（B-08）：与语音路径共用同一 runtime 与 store。

    命令状态先落库（回执 accepted/applied），这里只做设备动作并把回执推进到
    observed/failed；任何设备异常都如实回收，不假装成功。
    """

    runtime = build_runtime()
    service: ManagedService = runtime.service
    if kind != "play" and runtime.session_id != session_id:
        service.observe(receipt_id, state="failed",
                        detail="session is not bound to the device")
        return
    try:
        if runtime.session_id not in (None, session_id):
            # 单设备语义：换会话即接管——旧会话先停 tick，并**落 stopped**，
            # 否则旧客户端会一直看到「playing」而设备其实已被别人播。
            previous = runtime.session_id
            await runtime.stop()
            try:
                service.command(
                    previous, kind="stop", payload={"reason": "taken_over"},
                    principal="song_bridge",
                    idem_key=f"takeover-{previous}-{service.store.get(previous).epoch}",
                    target=target(),
                )
            except Exception:  # noqa: BLE001 - 旧会话已失效时不影响新会话
                pass
        if kind == "play":
            state = service.store.get(session_id)
            if state is None or not state.entries:
                raise ManagedError("queue is empty")
            runtime.bind(session_id)
            await runtime.player.start(session_id)
            await runtime.start()
        elif kind == "pause":
            runtime.bind(session_id)
            await runtime.player.pause_backend()
        elif kind == "resume":
            from core.music import focus_bridge

            runtime.bind(session_id)
            await focus_bridge.force_release()
            outcome = await runtime.player.ensure_resumed(session_id)
            if outcome not in ("resumed", "restarted"):
                raise ManagedError(f"resume outcome={outcome}")
            await runtime.start()
        elif kind in ("next", "prev"):
            runtime.bind(session_id)
            await runtime.player.start(session_id)
            await runtime.start()
        elif kind == "stop":
            from core.music import focus_bridge

            await focus_bridge.force_release()
            await runtime.stop()
            runtime.bind(None)
        else:
            raise ManagedError(f"unknown device command {kind!r}")
    except Exception as exc:  # noqa: BLE001 - 设备拒绝如实回报
        service.observe(receipt_id, state="failed", detail=str(exc)[:160])
        return
    service.observe(receipt_id)


def focus():
    """当前托管会话的 TTS focus 句柄（无会话时 None）。"""

    runtime = get_runtime()
    if runtime is None or runtime.service is None or not runtime.session_id:
        return None
    handle = getattr(runtime, "_tts_focus", None)
    if handle is None:
        from core.music.tts_focus import TtsFocus

        handle = TtsFocus(runtime.service, runtime.player)
        runtime._tts_focus = handle
    return handle


def active_session() -> str | None:
    runtime = get_runtime()
    return runtime.session_id if runtime is not None else None


async def status() -> dict | None:
    runtime = get_runtime()
    if runtime is None or not runtime.session_id:
        return None
    return runtime.service.snapshot(runtime.session_id)


def _is_active_managed(session_id: str | None) -> bool:
    runtime = get_runtime()
    return bool(runtime and runtime.session_id == session_id)


def _reset_for_tests() -> None:
    set_runtime(None)


# ---------- B-08：Songloft 托管准备链解析器 ----------
#
# "managed_media_resolver": "songloft" 时，托管队列当前曲不经 bridge 自查
# kwqq 两跳，而是驱动 Songloft 的 prepare→验证→票据链：
#   POST /api/v1/managed/prepare（202）→ 轮询 GET .../prepare（ready/failed）
#   → 返回票据化 /media?media_ticket=... 绝对 URL（设备直接拉流）。
# 媒体授权与网络控制分离：bridge 拿到的是短期可撤销票据 URL，不是管理员凭证。

_songloft_token_cache: dict = {}


def _songloft_cfg(key: str, default=None):
    import os

    val = _sr._cfg(key, default)
    if key == "songloft_password" and not val:
        val = os.environ.get("SONGLOFT_PASSWORD", "")
    return val


async def _songloft_token(base: str, http) -> str:
    """登录并缓存 JWT（7 天有效期，按过期时间提前 60s 刷新）。"""
    import time as _time

    cached = _songloft_token_cache.get("token")
    exp = _songloft_token_cache.get("exp", 0)
    if cached and _time.time() < exp - 60:
        return cached
    user = str(_songloft_cfg("songloft_user", "admin"))
    password = str(_songloft_cfg("songloft_password", ""))
    if not password:
        raise ManagedError("songloft resolver: SONGLOFT_PASSWORD not configured")
    async with http.post(
        f"{base}/api/v1/auth/login",
        json={"username": user, "password": password},
    ) as resp:
        if resp.status != 200:
            raise ManagedError(f"songloft login: http {resp.status}")
        data = await resp.json()
    token = str((data or {}).get("access_token") or "")
    if not token:
        raise ManagedError("songloft login: no access_token")
    _songloft_token_cache["token"] = token
    _songloft_token_cache["exp"] = _time.time() + 6.5 * 24 * 3600
    return token


async def _songloft_resolver(entry):
    """经 Songloft 托管准备链解析当前曲，返回票据化媒体绝对 URL。"""
    import asyncio

    import aiohttp

    base = str(_songloft_cfg("songloft_base", "") or "").rstrip("/")
    if not base:
        raise ManagedError("songloft resolver: songloft_base not configured")
    headers = {"Content-Type": "application/json"}
    session_id = "bridge-lx06"     # 桥接域固定会话；entry_id 区分出现
    epoch = 1                      # Songloft 侧换代由其自身代次管理
    entry_id = entry.entry_id

    async with aiohttp.ClientSession() as http:
        token = await _songloft_token(base, http)
        headers["Authorization"] = f"Bearer {token}"

        body = {"session": session_id, "epoch": epoch, "entry": entry_id,
                "ref": {"source": entry.ref.source, "id": entry.ref.id,
                        "extra": entry.ref.extra_dict}}
        async with http.post(f"{base}/api/v1/managed/prepare",
                             json=body, headers=headers) as resp:
            if resp.status != 202:
                raise ManagedError(f"songloft prepare: http {resp.status}")

        deadline = asyncio.get_event_loop().time() + 120
        while True:
            await asyncio.sleep(2)
            qs = f"session={session_id}&epoch={epoch}&entry={entry_id}"
            async with http.get(f"{base}/api/v1/managed/prepare?{qs}",
                                headers=headers) as resp:
                if resp.status != 200:
                    raise ManagedError(f"songloft prepare poll: http {resp.status}")
                data = await resp.json()
            state = str(data.get("state") or "")
            if state == "ready":
                media = str(data.get("media_url") or "")
                if not media:
                    raise ManagedError("songloft ready without media_url")
                # 顺带返回验证过的时长：HTTP/App 入队没有 duration_s 时，
                # EOF 判定靠它，否则播完永远 unknown、不推进。
                try:
                    duration = float(data.get("duration_s") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                return base + media, duration
            if state == "failed":
                raise ManagedError(f"songloft prepare failed: {data.get('error')}")
            if asyncio.get_event_loop().time() > deadline:
                raise ManagedError("songloft prepare timeout")
