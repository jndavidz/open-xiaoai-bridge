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


def _build_runtime() -> ManagedRuntime:
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
    player = ManagedQueuePlayer(store, RpcBackend(), _default_resolver)
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
    runtime = _build_runtime()
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
            backend_state = await runtime.player.backend.state()
            if backend_state == "paused":
                await runtime.player.resume_backend()
            else:
                # 唤醒/TTS 已把设备媒体上下文顶掉（idle/unknown）：设备控制 CLI 的 play
                # 对已丢失上下文是无操作，必须显式重新起播当前 entry（用户显式命令，不是偷偷播）。
                await runtime.player.restart(runtime.session_id)
            service.observe(receipt.receipt_id)
        elif kind in ("next", "prev"):
            await runtime.player.start(runtime.session_id)
            service.observe(receipt.receipt_id)
    except Exception as exc:  # noqa: BLE001 - 设备拒绝要如实回报，不假装成功
        service.observe(receipt.receipt_id, state="failed", detail=str(exc)[:160])
        return f"托管命令未能执行（{kind}）：{exc}"
    if kind == "stop":
        await runtime.stop()
        runtime.bind(None)
    return f"托管命令已受理（{kind}, {receipt.state}）"


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
