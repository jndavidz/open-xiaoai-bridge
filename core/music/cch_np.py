"""CCH now-playing 编排器（票 03 换代编排 / 票 04 TTS 恢复 / 票 13 收藏幂等，2026-11-20）。

数据面：cch_music_worker.py 采集 `CchBridgeMeta` META/STATE 事件 → 双写
proxy `/cch/np-ingest`（面板面）+ bridge `/api/external/np`（本编排面）。

编排语义（D+kwqq，facts §2.32/§2.34）：
- STATE `finish`/`stop` 且非我方发起 → **换代**：先逐条 revoke 刚播完那条的旧票
  （spec 裁决 4「换代旧票逐条 /managed/command stop 撤」，不依赖 NewEpoch），再 cast
  队列下一首 kwqq 曲目；不编排则原厂推荐队列自动连播接管（真机实证）。
  同一 finish/stop 在冷却窗内重复上报不二次 advance（原厂 stop 系事件可能连发）。
- STATE `suspend`（TTS 抢占）→ 记录恢复点；恢复动作 = resume 或重新 cast
  （试听档/外部 renderData 实例可能被语音链杀，facts §2.30）——恢复触发由
  `speak_with_resume()` 的桥内定时器（估算时长 + 静默余量，非轮询）执行。
- STATE `play` 且非我方 cast → 方案 b 接管观测点（是否接管待用户裁决，
  本版只记录不动作）。

META 事件更新内存态（Title/IconUri/isFavorite），供收藏幂等（票 13）与工具层读回。
队列与索引借用 song_request 的 `_cch_state`（单会话语义不变）。

焦点红线（票 04）：原厂 = 唯一播放器持焦点；本编排只发 9102 /cmd 与 /speak，
恒不 requestAudioFocus / 不 MediaPlayer（桥 APK v0.3.0 起已切除，两侧一致）。
"""
from __future__ import annotations

import asyncio

from core.utils.logger import logger

_np_state: dict = {"meta": {}, "state": {}}
_advance_lock = asyncio.Lock()
# 换代冷却窗：finish/stop 系事件可能连发（stop→finish 相邻上报），同一窗口只 advance 一次
_ADVANCE_COOLDOWN_S = 2.0
_last_advance_at: float = -1e9
# 票 04：/speak 恢复定时器（桥内定时器，非轮询）；新 speak 顶掉旧定时器
_speak_timer: asyncio.TimerHandle | None = None


def np_enabled() -> bool:
    from core.tools import song_request as sr

    from core.music.cch_cast import np_enabled as cast_np_enabled

    return cast_np_enabled() and bool(sr._cfg("np_enabled", False))


def snapshot() -> dict:
    """当前内存态（META/STATE 最新 extras）。"""
    return {"meta": dict(_np_state["meta"]), "state": dict(_np_state["state"])}


def record_event(kind: str, extras: dict, device_ts: str = "") -> None:
    """编排关闭（双关）时只记录不动作。

    语义修正（票 13 服务端收尾）：双关分支此前只调 snapshot() 纯读、extras 不进内存，
    isFavorite 回读无数据 → 收藏幂等无从谈起。现在双关时 META/STATE 仍记录，
    只是 finish/stop 不触发 advance、suspend 不登记恢复点。

    键归一：worker 上行 kind = "META"/"STATE"（大写），内存态键 = "meta"/"state"
    （snapshot/_latest_state_str 的读方）——此前 on_np_event 直接以大写 kind 存键，
    读方永远读空（上批遗留 bug，本批修正）。
    """
    _np_state[kind.lower()] = {"extras": extras or {}, "device_ts": device_ts}


async def on_np_event(kind: str, extras: dict, device_ts: str = "") -> dict:
    """worker 上行事件入口。返回编排动作（供日志/审计）。"""
    _np_state[kind.lower()] = {"extras": extras or {}, "device_ts": device_ts}
    if kind != "STATE":
        return {"action": "recorded", "kind": kind}
    state_str = str((extras or {}).get("statusStr") or "").lower()
    code = str((extras or {}).get("audioStatus") or "")
    if state_str in ("finish", "stop") and code in ("1", "8"):
        return await _advance_if_finished()
    if state_str == "suspend":
        # 票 04：恢复点由 speak_with_resume 的定时器触发，此处只记录
        return {"action": "suspend_recorded"}
    if state_str == "play":
        return {"action": "play_recorded"}
    return {"action": "recorded", "state": state_str or code}


async def _advance_if_finished() -> dict:
    """kwqq 曲目播完 → 逐条撤旧票 + cast 队列下一首。队列耗尽 → 回退原厂链（记录，不动作）。"""
    global _last_advance_at
    async with _advance_lock:
        from core.tools import song_request as sr

        loop = asyncio.get_event_loop()
        now = loop.time()
        if now - _last_advance_at < _ADVANCE_COOLDOWN_S:
            logger.debug("[CchNp] advance suppressed (cooldown)", module="CchNp")
            return {"action": "advance_cooldown"}
        items = sr._cch_state.get("items") or []
        idx = int(sr._cch_state.get("entry", 0))
        # 票 23 单发标志：点击播 = 单曲意图，曲终不换代（防搜索变体连播）。
        # 标志由点击播设置、下一次语音点歌路径显式清除。
        if sr._cch_state.get("no_advance"):
            logger.info("[CchNp] advance suppressed (no_advance single-shot)", module="CchNp")
            _last_advance_at = now
            return {"action": "no_advance"}
        # 旧票逐条撤（spec 裁决 4）：尽力而为，失败不阻塞 advance
        if idx < len(items):
            await _revoke_entry(idx, items[idx])
        nxt = idx + 1
        if nxt >= len(items):
            logger.info(f"[CchNp] queue exhausted (played {idx + 1}/{len(items)}) — fallback to native chain",
                        module="CchNp")
            _last_advance_at = now
            return {"action": "queue_exhausted"}
        item = items[nxt]
        sr._cch_state["entry"] = nxt
        try:
            from core.music.cch_cast import (
                build_render_data,
                cast_native,
                host_cmd,
                host_token_deploy,
                kwqq_meta,
                m5_enabled,
                prepare_playback,
                resolve_ticket_url,
                wait_np_play,
            )

            ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
                   "extra": item.get("extra") or {}}
            ticket_url = await resolve_ticket_url(
                ref, session=sr._CCH_SESSION, epoch=sr._CCH_EPOCH,
                entry=str(item.get("id") or f"cch-{nxt}"))
            meta = await kwqq_meta(ref["id"], ref.get("source") or "kuwo")
            render_data = build_render_data(ticket_url, item, meta)
            if m5_enabled():
                await host_token_deploy()
                ok, status = await host_cmd("play", url=ticket_url)
                detail = status
            else:
                # 活实例轨：曲末实例死 ⇒ cast 大概率无声；事后验证 NEED_M5 感知
                ok, detail = await cast_native(render_data)
                if ok and not await wait_np_play(5.0):
                    ok, detail = False, "NEED_M5"
            # 票 21：UI 队列在点歌时整批下发一次（保持搜索原序）。曲末 advance 不重写队列——
            # 宿主 QueueWriter 只有 replace 无"设置选中项"op，重排会破坏 spec「顺序一致」；
            # 原厂列表高亮是否自动跟随播放曲待真机验证（票 21 Comments 挂账）。
            _last_advance_at = loop.time()
            logger.info(f"[CchNp] advance {idx}->{nxt} ok={ok} detail={detail}", module="CchNp")
            return {"action": "advanced", "index": nxt, "ok": ok}
        except Exception as exc:  # noqa: BLE001
            _last_advance_at = loop.time()
            logger.error(f"[CchNp] advance err: {type(exc).__name__}: {exc}", module="CchNp")
            return {"action": "advance_failed", "error": type(exc).__name__}


async def _revoke_entry(idx: int, item: dict) -> bool:
    """撤单条旧票（/managed/command stop）。失败仅 warning（票据 30min TTL 自过期兜底）。"""
    try:
        from core.music.cch_cast import revoke

        entry_id = str(item.get("id") or f"cch-{idx}")
        ok = await revoke(sr_session(), sr_epoch(), entry_id)
        if not ok:
            logger.warning(f"[CchNp] revoke entry={entry_id} failed", module="CchNp")
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[CchNp] revoke err: {type(exc).__name__}", module="CchNp")
        return False


def sr_session() -> str:
    from core.tools import song_request as sr

    return sr._CCH_SESSION


def sr_epoch() -> int:
    from core.tools import song_request as sr

    return sr._CCH_EPOCH


def _latest_state_str() -> str:
    extras = (_np_state.get("state") or {}).get("extras") or {}
    return str(extras.get("statusStr") or "").lower()


async def resume_after_tts() -> dict:
    """TTS 结束后恢复 kwqq 播放（票 04）：先 resume，STATE 8（实例死）→ 重新 cast。

    恢复点判定（估算时长 + 静默余量）由 speak_with_resume 的定时器负责，本函数只执行动作。
    幂等：STATE 已回到 play（原厂自行 resume，facts §2.30 真人实测正常）→ 不再动作。
    """
    if _latest_state_str() == "play":
        return {"action": "already_playing"}
    from core.music.cch_cast import cast_native_simple

    ok, detail = await cast_native_simple(2)
    if ok:
        return {"action": "resumed"}
    # resume 打在死实例上 → 重新 cast 当前曲目（票据 30min 内，过期 re-prepare）
    return await _recast_current()


async def _recast_current() -> dict:
    from core.tools import song_request as sr

    items = sr._cch_state.get("items") or []
    idx = int(sr._cch_state.get("entry", 0))
    if idx >= len(items):
        return {"action": "recast_skipped", "reason": "no current item"}
    item = items[idx]
    try:
        from core.music.cch_cast import (
            build_render_data,
            cast_native,
            kwqq_meta,
            prepare_playback,
            resolve_ticket_url,
        )

        ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
               "extra": item.get("extra") or {}}
        ticket_url = await resolve_ticket_url(
            ref, session=sr._CCH_SESSION, epoch=sr._CCH_EPOCH,
            entry=str(item.get("id") or f"cch-{idx}"))
        meta = await kwqq_meta(ref["id"], ref.get("source") or "kuwo")
        render_data = build_render_data(ticket_url, item, meta)
        await prepare_playback()  # 预清残留 + 条件重注册（facts §2.48）
        ok, detail = await cast_native(render_data)
        return {"action": "recast", "ok": ok, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"action": "recast_failed", "error": type(exc).__name__}


# ---- 票 04：我方 TTS 抢占编排（/speak + 桥内定时器恢复，非轮询） ----

def _schedule_resume(delay_s: float) -> None:
    """登记恢复定时器；新 speak 顶掉旧定时器（连播两段播报只恢复一次）。"""
    global _speak_timer
    loop = asyncio.get_running_loop()
    if _speak_timer is not None:
        _speak_timer.cancel()
    _speak_timer = loop.call_later(max(0.05, float(delay_s)), _fire_resume)


def _fire_resume() -> None:
    loop = asyncio.get_running_loop()
    loop.create_task(_resume_if_pre_active())


async def _resume_if_pre_active() -> dict:
    """定时器到点：pre 活跃（队列仍在）且 STATE 未自行恢复 → 执行恢复动作。"""
    from core.tools import song_request as sr

    if not (sr._cch_state.get("items")):
        return {"action": "resume_skipped", "reason": "no queue"}
    if _latest_state_str() == "play":
        return {"action": "already_playing"}
    result = await resume_after_tts()
    logger.info(f"[CchNp] tts resume action={result.get('action')}", module="CchNp")
    return result


async def speak_with_resume(text: str, *, delay_s: float | None = None) -> dict:
    """我方 TTS 经 9102 /speak 播报 + 播完恢复 kwqq（票 04 任务 2/4）。

    双条件语义修正：票 04 原文「字数估算时长 + KWS 静默超时」出自 LX06 语境；
    CCH 侧我方无 KWS、原厂 TTS 结束不可观测（/speak 返回值只是 rc），
    故实现 = 文本估算预算（`estimate_playback_budget_ms`：4.5 字/秒 + 6s 静默余量）
    单定时器；到点后再查 STATE，原厂已自行 resume 则跳过（不重复 resume）。

    pre 判据 = np_enabled 且 song_request 队列非空（kwqq 在管）。不在管 → 只播报不编排
    （原厂播放的原厂 TTS 抢占由原厂自己 suspend/resume，facts §2.30）。

    delay_s 仅供离线测试注入（默认 None = 估算）。
    """
    from core.music.cch_cast import speak as cch_speak
    from core.tools import song_request as sr

    pre_active = bool(np_enabled()) and bool(sr._cch_state.get("items"))
    ok, detail = await cch_speak(text)
    if not ok:
        return {"action": "speak_failed", "detail": detail}
    if not pre_active:
        return {"action": "speak_only"}
    if delay_s is None:
        from core.services.audio.playback_timing import estimate_playback_budget_ms

        delay_s = estimate_playback_budget_ms(text, None) / 1000.0
    _schedule_resume(delay_s)
    return {"action": "speak_scheduled_resume", "delay_s": round(float(delay_s), 2)}


# ---- 票 13：收藏（Songloft 服务端为唯一落点，2026-11-21 用户裁决） ----


async def apply_favorite(item: dict, intent: bool, playlist_id=None,
                         song_id: int | None = None) -> dict:
    """收藏写入统一实现（票 13 → 2026-11-25 架构统一）：ensure_song → 入单/出单 → push。

    **所有收藏消费者共用此唯一写入口**：语音 cch_collect / 屏上爱心 HEART 传 item
    （内部 ensure_song）；收藏 tab 红心 FAVADD/FAVREMOVE 已按 rawID 反查到 song_id，
    传 song_id 跳过 ensure_song（避免 REMOVE 时 upsert 出新 id）。
    歌单恒在 Songloft 服务端（唯一真相），设备 favorites.json 只是镜像。
    """
    try:
        import aiohttp

        from core.music import songloft_api

        async with aiohttp.ClientSession() as http:
            if song_id is None:
                song_id = await songloft_api.ensure_song(item, http=http)
            extra = {"playlist_id": playlist_id} if playlist_id is not None else {}
            if intent:
                res = await songloft_api.add_to_favorites(song_id, http=http, **extra)
                action = "added" if int(res.get("added") or 0) > 0 else "noop"
            else:
                await songloft_api.remove_from_favorites(song_id, http=http, **extra)
                action = "removed"
    except Exception as exc:  # noqa: BLE001 - Songloft 不可用时明确失败，不回退原厂
        logger.warning(f"[CchNp] apply_favorite failed: {type(exc).__name__}: {exc}",
                       module="CchNp")
        return {"action": "failed", "detail": type(exc).__name__, "favorite": None}
    # 票 25：收藏写成功后同步设备收藏文件（fire-and-forget，fail-open）。
    if action in ("added", "removed"):
        from core.music import cch_cast

        asyncio.ensure_future(cch_cast.push_favorites())
    return {"action": action, "favorite": bool(intent), "playlist_id": playlist_id}


async def current_song() -> dict | None:
    """设备侧当前曲（票 13 架构）：经 9102 currentsong 读 capture worker 写的
    current_song.json——15s 轮询、滞后，仅作收藏的设备侧兜底源（§2.77-51）：
    常态真相源是内存队列 _cch_state.items[entry]（见 _current_from_cch_queue）。
    文件不存在/未就绪 → None（fail-open）。"""
    from core.music import cch_cast

    try:
        ok, payload = await cch_cast.musicctl("currentsong")
        if ok and isinstance(payload, dict):
            return payload.get("song")
    except Exception as exc:  # noqa: BLE001 - 读取失败降级为无当前曲
        logger.warning(f"[CchNp] currentsong read err: {type(exc).__name__}: {exc}",
                       module="CchNp")
    return None


def _current_from_cch_queue() -> dict | None:
    """收藏的当前曲真相源 = bridge 内存队列 entry 位置的门面 item（§2.77-51）。

    current_song.json 由 worker 15s 轮询写，点播后立即收藏会停在旧歌（真机已复现
    「播风吹麦浪收藏成晴天」）；而 _cch_state.items 在点播时即时更新，item 携带门面
    完整字段（id/name/artist/album/cover 原图/source 真实平台/duration_s）——与手机
    App toRemotePayload 写入格式一致。故收藏以队列 item 为权威，current_song 仅兜底。
    队列空/异常 → None，由调用方回退设备侧 current_song。"""
    try:
        from core.tools import song_request as sr

        items = sr._cch_state.get("items") or []
        if not items:
            return None
        entry = int(sr._cch_state.get("entry", 0))
        if not (0 <= entry < len(items)):
            entry = 0
        item = items[entry]
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            return None
        # ensure_song 消费的字段集：name/artist/singer/album/cover/source/id/duration_s
        cover = str(item.get("cover") or "").strip()
        # scheme 守卫（纵深防御）：只接受门面原图 http(s) URL，拒绝相对路径/虚化图
        # （/api/cch/bg/）——后者写进 Songloft 会变成无效封面。
        if not (cover.startswith("http://") or cover.startswith("https://")):
            cover = ""
        song = {
            "id": str(item.get("id") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "source": str(item.get("source") or "").strip(),
            "artist": str(item.get("artist") or item.get("singer") or "").strip(),
            "album": str(item.get("album") or "").strip(),
            "cover": cover,
            "duration_s": float(item.get("duration_s") or 0),
        }
        if not song["name"] or not song["source"]:
            return None
        logger.debug(f"[CchNp] collect current from queue: id={song['id']} "
                     f"source={song['source']} cover={bool(song['cover'])}", module="CchNp")
        return song
    except Exception as exc:  # noqa: BLE001 - 队列读取失败回退设备侧 current_song
        logger.debug(f"[CchNp] queue current read failed: {type(exc).__name__}", module="CchNp")
        return None


async def collect_favorite(intent: bool, playlist_id=None) -> dict:
    """收藏（票 13）：以内存队列 entry item 为当前曲真相源（§2.77-51，点播即时准确），
    设备侧 current_song.json（worker 15s 轮询，滞后）仅在队列空时兜底。写入走统一
    apply_favorite——语音 cch_collect 与屏上爱心 cch_click 同一入口同一实现。"""
    song = _current_from_cch_queue()
    if song is None:
        song = await current_song()
    if not song or not song.get("id") or not song.get("source"):
        return {"action": "no_track", "favorite": None}
    return await apply_favorite(song, intent, playlist_id)
