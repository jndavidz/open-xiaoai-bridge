"""旧 `song_request` → 托管队列的迁移桥（B-06，flag 后启用）。

开关：`song_request.managed`（config，默认 false）。关 = 旧内存队列原样工作
（回滚模式）；开 = 搜索/专辑候选逻辑照旧复用，**队列状态改由托管 store 持有**，
播放推进由 `ManagedRuntime` 的 tick 驱动。

铁律：同一时刻只有一条队列所有权——flag 打开后旧 `_state` 不再维护队列语义，
不会出现「两个自动推进」。
"""

from __future__ import annotations

from core.music.backends import DlnaBackend
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
    service = ManagedService(store=store)
    player = ManagedQueuePlayer(store, DlnaBackend(), _default_resolver)
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
    snapshot = service.open_session(target=target(), transport="dlna",
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
    if kind in ("next", "prev"):
        await runtime.player.start(runtime.session_id)
    if kind == "stop":
        await runtime.stop()
        runtime.bind(None)
    return f"托管命令已受理（{kind}, {receipt.state}）"


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
