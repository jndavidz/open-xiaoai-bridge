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
import random
import time

from core.utils.logger import logger

_np_state: dict = {"meta": {}, "state": {}}
_advance_lock = asyncio.Lock()
# 换代冷却窗：finish/stop 系事件可能连发（stop→finish 相邻上报），同一窗口只 advance 一次
_ADVANCE_COOLDOWN_S = 2.0
_last_advance_at: float = -1e9
# 乱序模式 RNG（模块级单例；rng 参数可注入做确定性测试）
_rng = random.Random()
# 票 04：/speak 恢复定时器（桥内定时器，非轮询）；新 speak 顶掉旧定时器
_speak_timer: asyncio.TimerHandle | None = None


# 播控页播放模式（票 11）：由 worker 抓 cmdVal playMode 经 HTTP 上报 set_play_mode；
# 缺省 normal（顺序）。批量点播读此值决定换代行为，单曲点播强制 single。
_current_play_mode: str = "normal"


def set_play_mode(mode: str) -> None:
    """设备播控页 playMode 上报入口（Normal/Shuffle/Repeat/RepeatOne → 内部小写）。
    仅接受已知四档，未知值忽略（fail-safe 保留上次有效值）。"""
    global _current_play_mode
    m = str(mode or "").strip().lower()
    if m in ("normal", "shuffle", "repeat", "repeatone"):
        _current_play_mode = m


def current_play_mode() -> str:
    return _current_play_mode


def choose_next(mode: str, queue_len: int, idx: int, played: set[int],
                 *, rng: random.Random | None = None) -> int | None:
    """五模式下一首选择（纯函数，可单测）。返回 None = 停止不推进。

    mode: single(单曲一次) / normal(顺序) / shuffle(乱序) / repeat(循环) / repeatone(单曲循环)
    played: 已播过的 index 集合（shuffle 用，避免重复）
    rng: 可注入随机源（确定性测试用），缺省用模块级 _rng——依赖注入而非测试后门。
    """
    if queue_len <= 0:
        return None
    idx = idx if 0 <= idx < queue_len else 0
    mode = (mode or "normal").strip().lower()

    if mode == "single":
        return None
    if mode == "repeatone":
        return idx
    if mode == "repeat":
        return (idx + 1) % queue_len
    if mode == "shuffle":
        pool = [i for i in range(queue_len) if i not in played]
        if not pool:  # 全部播过 → 重置池
            pool = list(range(queue_len))
        return (rng or _rng).choice(pool)
    # normal 及未知模式回退顺序
    return idx + 1 if idx + 1 < queue_len else None


def np_enabled() -> bool:
    """换代编排开关 = cch_np_enabled 单一权威（票 02/11 真机修正）。

    旧实现额外要求 sr._cfg("np_enabled", False)——生产配置里根本没有这个 key
    （配置写的是 cch_np_enabled），恒 False → 曲终只记录不换代（recorded_only），
    「播一首就停」根因之一。cch_cast.np_enabled() 已读 cch_np_enabled，直接复用即可。
    """
    from core.music.cch_cast import np_enabled as cast_np_enabled

    return cast_np_enabled()


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
    _np_state[kind.lower()] = {"extras": extras or {}, "device_ts": device_ts,
                               "observed_at": time.time()}


async def on_np_event(kind: str, extras: dict, device_ts: str = "") -> dict:
    """worker 上行事件入口。返回编排动作（供日志/审计）。"""
    _np_state[kind.lower()] = {"extras": extras or {}, "device_ts": device_ts,
                               "observed_at": time.time()}
    if kind != "STATE":
        return {"action": "recorded", "kind": kind}
    state_str = str((extras or {}).get("statusStr") or "").lower()
    code = str((extras or {}).get("audioStatus") or "")
    # 换代只认自然曲终 finish（audioStatus:1）。stop（audioStatus:8）= 点播打断 /
    # TTS 抢占 / 手动停止——2026-11-26 用户报「批量点播首曲几秒切到第二首」，
    # 真机证实是点播后 TTS 播报产生的 stop 误触发了换代。
    if state_str == "finish" and code == "1":
        from core.music.playback_session import for_renderer

        return await for_renderer("cch").report("track_finish")
    if state_str == "stop":
        return {"action": "stop_recorded"}
    if state_str == "suspend":
        # 票 04：恢复点由 speak_with_resume 的定时器触发，此处只记录
        return {"action": "suspend_recorded"}
    if state_str == "play":
        return {"action": "play_recorded"}
    return {"action": "recorded", "state": state_str or code}


async def _pick_direct_url(item: dict) -> str | None:
    """票据链失败后的多源直链兜底（与 song_request._try_item 同策略）。

    复用 song_request 的 _sources/_pick_url（同一份源序与预览/加密件过滤），
    不在本模块重写源策略；无可用直链返回 None，由调用方决定抛错还是换候选。
    """
    import aiohttp
    from core.tools import song_request as sr

    async with aiohttp.ClientSession() as http:
        for src in sr._sources(None):
            url = await sr._pick_url(http, src, item)
            if url:
                return url
    return None


async def _prepare_cast(idx: int, item: dict, *, fallback_direct: bool = False) -> tuple[str, dict]:
    """按 (source,id) 取可播 URL + meta + renderData（advance/replay/recast 共用的准备链）。

    返回 (play_url, render_data)；下行策略（m5 / cast_native / prepare_playback）
    由各调用方决定——准备链收敛，执行链保持各自语义。

    fallback_direct=True（仅 stopped replay 开启）：票据链与首次点播
    （song_request._try_item）同构——Songloft managed prepare 可能因音源数据本身被拒
    （如尾部无标签 junk 触发 verify.go:100），此时降级多源直链，不让曲终按 ▶️ 哑火
    （票 06 复现：爱你一万年）。换代链不开此兜底：prepare 失败须显式 advance_failed
    推进到下一曲，不能卡在当前曲重取直链（既有契约，见 test_prepare_exception）。
    """
    from core.music.cch_cast import (
        build_render_data, kwqq_meta, prepare_background, resolve_ticket_url,
    )
    from core.tools import song_request as sr

    ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
           "extra": item.get("extra") or {}}
    try:
        play_url = await resolve_ticket_url(
            ref, session=sr._CCH_SESSION, epoch=sr._CCH_EPOCH,
            entry=str(item.get("id") or f"cch-{idx}"))
    except Exception as exc:  # noqa: BLE001
        if not fallback_direct:
            raise  # 换代链保留原失败语义（advance_failed）
        logger.warning(f"[CchNp] ticket chain failed ({type(exc).__name__}); fallback direct",
                       module="CchNp")
        play_url = await _pick_direct_url(item)
        if not play_url:
            raise  # 无可用直链 → replay_failed（不吞错）
    meta = await kwqq_meta(ref["id"], ref.get("source") or "kuwo")
    # 换代也要重算虚化背景（2026-11-26 用户报「切歌后全屏背景没虚化」）：
    # 点播链原本有 bg_cache.ensure，换代链缺 ⇒ build_render_data 回落清晰封面。
    bg_url = await prepare_background(str(meta.get("cover") or item.get("cover") or ""))
    return play_url, build_render_data(play_url, item, meta, background_image=bg_url)


async def _cast_entry(idx: int, item: dict, *, fallback_direct: bool = False) -> dict:
    """按 (source,id) 准备 + cast 单曲（advance 与 stopped replay 共用）。

    返回 {ok, detail}；不改 _cch_state.entry（由调用方决定推进还是重播），不撤票。
    fallback_direct 透传给 _prepare_cast（仅 replay 开启，见其 docstring）。
    """
    from core.music.cch_cast import (
        attach_lyric,
        cast_native,
        host_cmd,
        host_token_deploy,
        m5_enabled,
        render_display,
        wait_np_play,
    )

    ticket_url, render_data = await _prepare_cast(idx, item, fallback_direct=fallback_direct)
    if m5_enabled():
        await host_token_deploy()
        ok, status = await host_cmd("play", url=ticket_url)
        # 架构收敛：与点播同一观测裁决——回执 TIMEOUT 不算失败，
        # 设备 STATE play 新鲜观测才是起播真相（真机 advance/replay 常 TIMEOUT 但已播）
        from core.music import playback_observation as obs
        from core.music.playback_session import for_renderer

        if await obs.observe_play(timeout_s=3.0,
                                  since_ts=for_renderer("cch").observation_since()):
            ok, status = True, "observed"
        detail = status
        # 换代/重播必须刷新歌词与显示层（2026-11-26 用户报「歌词对不上声音」）：
        # m5 轨 host_cmd 只传 URL、不带 renderData，设备显示层会沿用上一首的 lrc/封面。
        # 歌词注入先于显示补投，顺序错误会让本曲 lyricUrl 不进 render。
        render_data = await attach_lyric(item, render_data)
        await render_display(render_data)
    else:
        # 活实例轨：曲末实例死 ⇒ cast 大概率无声；事后验证 NEED_M5 感知
        # 同样要注入歌词（m5 关的回滚模式下换代也不能沿用旧 lrc）
        render_data = await attach_lyric(item, render_data)
        ok, detail = await cast_native(render_data)
        if ok and not await wait_np_play(5.0):
            ok, detail = False, "NEED_M5"
    return {"ok": ok, "detail": detail}


async def replay_current() -> dict:
    """票 06：曲终停止态点播放键 → 重新 cast 当前 entry（不推进队列、不换 entry）。

    单曲点播曲终后 native 实例死，host_cmd("toggle") 的 isPlaying?pause:resume 对死实例
    无效；重播必须重新取票 + cast（与 advance 共用 _cast_entry seam）。
    """
    from core.tools import song_request as sr

    items = sr._cch_state.get("items") or []
    idx = int(sr._cch_state.get("entry", 0))
    if not items or idx >= len(items):
        return {"action": "replay_no_item"}
    try:
        res = await _cast_entry(idx, items[idx], fallback_direct=True)
        logger.info(f"[CchNp] replay {idx} ok={res['ok']} detail={res['detail']}", module="CchNp")
        return {"action": "replayed", "index": idx, "ok": res["ok"]}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[CchNp] replay err: {type(exc).__name__}: {exc}", module="CchNp")
        return {"action": "replay_failed", "error": type(exc).__name__}


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
        # 票 23 单发标志：点击播 = 单曲意图，曲终不换代（防搜索变体连播）——
        # 与五模式的 single 等价，保留兼容。
        if sr._cch_state.get("no_advance"):
            logger.info("[CchNp] advance suppressed (no_advance single-shot)", module="CchNp")
            _last_advance_at = now
            return {"action": "no_advance"}
        # 票 11：五模式换代。单一真相源——点播意图存 _cch_state.play_intent（single/batch，
        # 不可变）；播控页四档状态实时读 current_play_mode()（worker 上报，可切换）。
        # 不再把四档值快照进队列，避免「点播后切按钮不生效」的双真相源问题。
        intent = str(sr._cch_state.get("play_intent") or "batch").lower()
        mode = "single" if intent == "single" else current_play_mode()
        played = set(sr._cch_state.get("played") or ())
        played.add(idx)
        nxt = choose_next(mode, len(items), idx, played)
        sr._cch_state["played"] = list(played)
        # 旧票逐条撤（spec 裁决 4）：无论推进还是停止，都撤刚播完那条的旧票
        if idx < len(items):
            await _revoke_entry(idx, items[idx])
        if nxt is None:
            reason = "single_once" if mode == "single" else (
                     "queue_exhausted" if mode == "normal" else "stop")
            logger.info(f"[CchNp] advance stop mode={mode} reason={reason} "
                        f"(played {len(played)}/{len(items)})", module="CchNp")
            _last_advance_at = now
            return {"action": reason}
        item = items[nxt]
        sr._cch_state["entry"] = nxt
        try:
            res = await _cast_entry(nxt, item)
            # 票 21：UI 队列在点歌时整批下发一次（保持搜索原序）。曲末 advance 不重写队列——
            # 宿主 QueueWriter 只有 replace 无"设置选中项"op，重排会破坏 spec「顺序一致」；
            # 原厂列表高亮是否自动跟随播放曲待真机验证（票 21 Comments 挂账）。
            _last_advance_at = loop.time()
            logger.info(f"[CchNp] advance {idx}->{nxt} ok={res['ok']} detail={res['detail']}",
                        module="CchNp")
            return {"action": "advanced", "index": nxt, "ok": res["ok"]}
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
        from core.music.cch_cast import cast_native, prepare_playback

        _, render_data = await _prepare_cast(idx, item)
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
    # 统一经 session 裁决：PREPARING/SILENCED 期间不恢复旧曲
    from core.music.playback_session import for_renderer

    result = await for_renderer("cch").report("tts_resume")
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
