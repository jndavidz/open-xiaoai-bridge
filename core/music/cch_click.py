"""CCH 历史点击事件 worker（票 23，design/21-23-device-module.md §2）。

数据流：:core cp_hook 命中云端 ContentPlay → 摘要 spool（设备文件，无 audioUrl）
→ tts_http :9102 `clickpoll`（读即删）→ 本 worker（1s 轮询）→ 时间窗去重
→ 复用语音点歌同一条链（`song_request.run` target=cch：早停 + 播报 + 取票 + M5 play），
不另写简化链（design §2.6：避免行为分叉）。

判别纪律（design §2.1）：native 无法区分「语音点歌的云端 ContentPlay」与「历史
点击」——同源。故 native 一律上报、本侧按时间窗去重；主判据 = 与最近一次主动
点播的归一化 name+artist 相同且在 `cch_click_dedupe_s`（默认 8s）内 → drop。
**不要**拿试听流 URL 特征当判据（两者同为试听流，无法区分）。

红线（design §7）：不 new MediaPlayer / 不 requestAudioFocus（票 16）；play 走
原厂内核；audioUrl/票据不进本模块日志；worker 自身起播也登记占窗（CP+createlist
同曲双发的实测形态，票 23 §2.71）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque

from core.utils.logger import logger

_POLL_S = 1.0            # design §2.6：1s 轮询
_SLOW_POLL_S = 5.0       # 连续失败降频
_FAIL_STREAK_SLOW = 3
_RECV_MAX = 10           # 单次 clickpoll 拉取上限（与 tts_http 默认一致）
_DEDUPE_CAP = 32         # design §2.6：deque 容量 32
_DEDUPE_DEFAULT_S = 8.0
_DEBOUNCE_DEFAULT_S = 3.0  # last-tap-wins 去抖（票 23 双发实测；cch_click_debounce_s 可配）
_RESUME_AFTER_S = 1.5    # design §2.7：起播后 1.5s 读 state，paused 补一次 resume

# (ts, norm_name, norm_artist) 环形窗——每次主动点播（语音 + worker 自身）前登记
_recent: deque = deque(maxlen=_DEDUPE_CAP)

# 最近一次语音输入（external text / ASR）时间戳（2026-11-23）：cp_hook 在 WS 原始字节层
# 无法区分「用户触屏点列表」与「云端对语音的 ContentPlay 应答」（两者 CP 结构相同）。
# 判据：语音应答必有 external text，故 12s 内有语音输入时忽略 cp_hook 的 NE/KG 点击事件，
# 让语音点歌走 LLM→song_request 单链；触屏点击（无近期语音）照常放行。
_recent_voice_ts: float = 0.0
_VOICE_GUARD_S = 12.0
_CP_SOURCES = frozenset({"NE", "KG", "KWQQ", "KUGOU", "NETEASE", "QQ", "MIGU", "QIANQIAN"})

# 收藏 tab 点击（§2.77-45）：tap_hook 写 source=FAVCLICK（用户实际点的收藏曲），
# 随后 cp_hook 会写云端错歌 spool（NE/KG，如"金刚经"）。收到 FAVCLICK 后短时间内
# drop 云端源 spool，避免双链竞速让错歌覆盖正确曲。
_recent_favclick_ts: float = 0.0
_FAVCLICK_GUARD_S = 3.0

# 屏上播放键去抖（§2.77 四问题①）：同一次点击两种 tag 各打一条 playOrPause error →
# 两条 PLAYTOGGLE spool；1s 内重复直接丢弃，防并发 toggle 两次翻转回原状。
_recent_playtoggle_ts: float = 0.0
_PLAYTOGGLE_DEBOUNCE_S = 1.0


def note_voice_input() -> None:
    """由 /api/external/text 在收到 ASR 文本时调用，标记"近期有语音输入"。"""
    global _recent_voice_ts
    _recent_voice_ts = time.time()


# 去抖状态：未决事件 + 其计时任务；播放链串行锁（song_request.run ~35s）
_pending_ev: dict | None = None
_pending_task: asyncio.Task | None = None
_play_lock = asyncio.Lock()

# 尾部括注（全/半角括号）：历史条目名常带「(四段)」「（Live）」等版本后缀
_BRACKET_RE = re.compile(r"[（(][^）)]*[）)]\s*$")


def _cfg(key, default=None):
    from core.tools import song_request as sr

    return sr._cfg(key, default)


def enabled() -> bool:
    """worker 总开关（cch_click_enabled，默认 False——模块 v0.4.9 刷机后由用户打开）。"""
    val = _cfg("cch_click_enabled", False)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _norm(text) -> str:
    """归一化：去尾部括注 + 去全部空白 + 小写（design §2.6「归一化 name+artist」）。"""
    s = _BRACKET_RE.sub("", str(text or "")).strip().lower()
    return re.sub(r"\s+", "", s)


def note_recent_play(name: str, artist: str) -> None:
    """登记一次主动点播。song_request 的 CCH 播放路径与 worker 自身起播都会调——
    时间窗内的云端回声 ContentPlay 由 dedupe() 判重丢弃。"""
    nn = _norm(name)
    if nn:
        _recent.append((time.time(), nn, _norm(artist)))


def dedupe(name: str, artist: str, *, now: float | None = None) -> str:
    """返回 ""=放行；非空=drop 原因。窗宽 cch_click_dedupe_s（默认 8s）。

    artist 单侧缺失按 name 相似判（audioAnchor 与搜索结果的歌手字段常不同构）。
    """
    ts = time.time() if now is None else now
    try:
        window = float(_cfg("cch_click_dedupe_s", _DEDUPE_DEFAULT_S) or _DEDUPE_DEFAULT_S)
    except (TypeError, ValueError):
        window = _DEDUPE_DEFAULT_S
    nn, na = _norm(name), _norm(artist)
    if not nn:
        return ""
    for ets, en, ea in reversed(_recent):        # deque 按时间升序，倒序首个超窗即停
        if ts - ets > window:
            break
        if en == nn and (not ea or not na or ea == na):
            return f"dedupe({en})"
    return ""


async def _resume_if_paused() -> None:
    """云端随后下发 Pause（§2.71-3 实测 20:45:22.105）会打断刚起的播：1.5s 后读
    np STATE，观察到 paused 才补一次 resume（只补一次，防抖）。"""
    await asyncio.sleep(_RESUME_AFTER_S)
    try:
        from core.music import cch_cast, cch_np

        st = (cch_np.snapshot().get("state") or {})
        status = str((st.get("extras") or {}).get("statusStr") or "").lower()
        if status in ("pause", "paused"):
            await cch_cast.host_cmd("resume")
            logger.info("[CchClick] resume after cloud pause", module="CchClick")
    except Exception as exc:  # noqa: BLE001 - 恢复是尽力而为，不阻塞 worker
        logger.warning(f"[CchClick] resume check err: {type(exc).__name__}", module="CchClick")


async def _handle_event(ev: dict) -> None:
    """兼容别名：直接播放（跳过去抖）。单测与外部调用沿用。"""
    await _play_event(ev)


async def _heart_collect(ev: dict) -> None:
    """屏上爱心（source=HEART_LIKE/UNLIKE）→ 语音收藏同一入口 cch_np.collect_favorite。

    爱心事件只携带意图（tap_hook 上报）；当前曲由 collect_favorite 经 9102 从设备侧
    current_song.json 读——语音收藏与屏上爱心收藏完全同一当前曲源、同一写入实现。"""
    from core.music import cch_np

    src = str(ev.get("source") or "")
    intent = src == "HEART_LIKE"
    result = await cch_np.collect_favorite(intent)
    logger.info(f"[CchClick] HEART {result.get('action')} intent={intent}", module="CchClick")


async def _fav_collect(ev: dict) -> None:
    """收藏 tab 红心点击（2026-11-25）：source=FAVADD/FAVREMOVE + rid=rawID。

    原厂 CollectRequest 走云端 modify，但我方收藏在 Songloft（不在原厂账号），
    云端回执 data.success=false → UI 乐观变🤍但实际没动。tap_hook 从 add-param
    抓 operation/rawId/source 写 spool，这里按 rawID 在 Songloft 收藏歌单找到 song_id，
    调 add/remove_from_favorites 真正改我方收藏（真相源），再 push 设备。
    """
    from core.music import songloft_api, cch_cast

    src = str(ev.get("source") or "")
    rid = str(ev.get("rid") or "").strip()
    intent = src == "FAVADD"
    if not rid:
        logger.warning("[CchClick] FAVADD/REMOVE missing rid, skip", module="CchClick")
        return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            pl_id = await songloft_api.favorites_playlist_id(http=http)
            songs = await songloft_api.playlist_songs(pl_id, http=http)
            song_id = None
            for s in songs:
                sd_raw = s.get("source_data")
                if not isinstance(sd_raw, str) or not sd_raw:
                    continue
                try:
                    sd = json.loads(sd_raw)
                except (ValueError, TypeError):
                    continue
                if str(sd.get("i") or "") == rid:
                    song_id = int(s["id"])
                    break
            if song_id is None:
                # 不在收藏列表（如 ADD 一首新歌）：从门面按 rawID 查不到就直接跳过，
                # 收藏 tab 的红心只应对已收藏曲做 REMOVE；ADD 通常是误触/云端曲。
                logger.info(f"[CchClick] FAV {src} rid={rid} not in favorites, skip",
                            module="CchClick")
                return
            if intent:
                await songloft_api.add_to_favorites(song_id, http=http)
            else:
                await songloft_api.remove_from_favorites(song_id, http=http)
        await cch_cast.push_favorites()
        logger.info(f"[CchClick] FAV {src} rid={rid} song={song_id} -> push done",
                    module="CchClick")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[CchClick] FAV {src} failed: {type(exc).__name__}: {exc}",
                       module="CchClick")


async def _histpos_play(ev: dict) -> None:
    """HISTPOS 点击 → 反查真相源 → 复用 FAVCLICK rawID 直构点播路径。"""
    global _recent_favclick_ts
    resolved = await _histpos_to_ev(ev)
    if resolved is None:
        return
    _recent_favclick_ts = time.time()   # 复用 favclick-guard 抑制云端错歌回声
    await _play_event(resolved)


async def _fav_name_by_rawid(raw_id: str) -> str:
    """按 rawID 从 Songloft 收藏歌单反查 display title（仅用于播报/兜底，
    点播身份始终是 rawID+source）。查不到返回空串，不影响点播。"""
    try:
        import aiohttp
        from core.music import songloft_api
        async with aiohttp.ClientSession() as http:
            pl = await songloft_api.favorites_playlist_id(http=http)
            songs = await songloft_api.playlist_songs(pl, http=http)
        for s in songs:
            sd_raw = s.get("source_data")
            if not isinstance(sd_raw, str) or not sd_raw:
                continue
            try:
                sd = json.loads(sd_raw)
            except (ValueError, TypeError):
                continue
            if str(sd.get("i") or "") == raw_id:
                return str(s.get("title") or sd.get("n") or "")
    except Exception:  # noqa: BLE001 - 仅播报名，fail-open
        pass
    return ""


async def _histpos_to_ev(ev: dict) -> dict | None:
    """历史 tab 点击（source=HISTPOS，rid=显示 pos）→ 从真相源 cch_history 按 pos
    反查 (rawID, source, name)，转成 FAVCLICK 同款 ev（rawID 直构点播）。
    歌曲唯一身份 = (source, rawID)，不依赖歌名搜索、不依赖 native ring pos 偏移。
    """
    from core.music import cch_history

    pos = ev.get("rid")
    hit = await cch_history.get_by_pos(pos)
    if not hit:
        logger.info(f"[CchClick] HISTPOS pos={pos} not in history truth source, skip",
                    module="CchClick")
        return None
    out = dict(ev)
    out["name"] = hit["name"] or "histpos"
    out["artist"] = hit["artist"]
    out["rid"] = hit["rawId"]
    out["album"] = hit["source"]
    out["source"] = "FAVCLICK"   # 复用已验证的 rawID 直构路径 + favclick-guard
    logger.info(f"[CchClick] HISTPOS pos={pos} -> rawID={hit['rawId']} "
                f"src={hit['source']} name={hit['name']!r}", module="CchClick")
    return out


async def _playtoggle(ev: dict) -> None:
    """屏上播放/暂停键（source=PLAYTOGGLE）：设备侧原子 toggle，桥侧零状态读取。

    原厂 playOrPause 因内核无我方数据源失败（tap_hook 抓 error 上报）。控制走 CCH 的
    M5 in-process 通道（cch_cast.host_cmd("toggle")）——设备侧 HostBootstrap 用
    isPlaying ? pause : resume 原子翻转，桥侧只读命令结果、不接触播放态存储。
    """
    from core.music import cch_cast

    # 设备侧 M5 原子 toggle（HostBootstrap.dispatch "toggle" = isPlaying ? pause : resume），
    # 桥侧零状态读取。
    ok, detail = await cch_cast.host_cmd("toggle")
    logger.info(f"[CchClick] playtoggle -> host_cmd(toggle) ok={ok} detail={detail}",
                module="CchClick")


def _accept_event(ev: dict) -> None:
    """事件入口（worker 拉取循环调用，同步）：源准入 → 去重 → 去抖 last-tap-wins。

    源准入（票 23 真机 01:11 实证）：只接受 `HIST`（tap hook join 的用户 UI 直拆
    意图）。`KG`（:core lws 抓的云端 CP）一律 drop——它要么是点击的队列头回声
    （HIST 已覆盖真意图），要么是语音搜索结果（语音链自己会播，worker 再播 =
    双链竞速「先后两个页面只播后一个」）。
    """
    global _pending_ev, _pending_task, _recent_favclick_ts, _recent_playtoggle_ts
    name = str(ev.get("name") or "")
    source = str(ev.get("source") or "")
    # 票 13 动作面：播放页爱心点击由 :business tap_hook 写 source=HEART_LIKE/HEART_UNLIKE
    # spool（只带意图，当前曲由 collect_favorite 从设备侧 current_song.json 读）。
    # 与语音收藏走同一入口，不走 HIST 的点播去抖链（那条链会改当前曲队列）。
    if source.startswith("HEART"):
        logger.info(f"[CchClick] HEART recv src={source} -> collect_favorite", module="CchClick")
        asyncio.create_task(_heart_collect(ev))
        return
    # 收藏 tab 红心点击（2026-11-25）：tap_hook 抓 CollectRequest add-param 写
    # FAVADD/FAVREMOVE + rid=rawID。原厂云端 modify 失败（data.success=false）UI 假变🤍，
    # 这里按 rawID 真正改 Songloft 收藏（我方真相源）+ push 设备。
    if source in ("FAVADD", "FAVREMOVE"):
        logger.info(f"[CchClick] {source} recv rid={ev.get('rid')} -> fav_collect",
                    module="CchClick")
        asyncio.create_task(_fav_collect(ev))
        return
    # 播放历史 tab 点击（架构统一）：tap_hook 只上报显示 pos（HISTPOS），
    # 这里从真相源 cch_history 按 pos 反查 (rawID, source, name)，转 FAVCLICK 同款
    # rawID 直构点播（不依赖歌名搜索 / native ring pos 偏移）。反查失败即跳过。
    if source == "HISTPOS":
        asyncio.create_task(_histpos_play(ev))
        return
    # 屏上播放/暂停键（§2.77 四问题①）：tap_hook 抓 playOrPause error → PLAYTOGGLE spool。
    # 读实时播放态反向切（speaker 控制通道），不走点播去抖链。
    if source == "PLAYTOGGLE":
        now = time.time()
        if now - _recent_playtoggle_ts < _PLAYTOGGLE_DEBOUNCE_S:
            logger.info("[CchClick] PLAYTOGGLE drop(debounce)", module="CchClick")
            return
        _recent_playtoggle_ts = now
        logger.info("[CchClick] PLAYTOGGLE recv -> playtoggle", module="CchClick")
        asyncio.create_task(_playtoggle(ev))
        return
    # 源准入：HEART / PLAYTOGGLE 已在上面 return；其余 source 都走 dedupe 判定。
    #  HIST = 历史 tab（title+createList join）；CLICK = tap_hook 抓的 PlayByIndex+ContentPlay
    #  （gate 漏拦时才有 App 日志，属冗余）；NE/KG/KWQQ... = cp_hook 在 :core 拦截云端
    #  ContentPlay 时提取（2026-11-23 VERIFIED：用户点当前播放列表时稳定上报，bridge 点播
    #  不走云端 ContentPlay 故不上报）。票 23 曾无条件 drop 这些"云端回声"是因为那时历史 tab
    #  有 HIST 覆盖真意图；当前播放 tab 没有 HIST，drop 会致点击无声——改为靠 dedupe +
    #  note_recent_play（主动点播 8s 占窗）区分回声，无登记的用户点击放行。
    # 收藏 tab 点击（§2.77-45）：FAVCLICK = tap_hook 从 playFavorite 点击 pos join ring 得到
    # 的用户实际点的收藏曲（正确数据源）；记时间戳用于抑制随后 cp_hook 的云端错歌 spool。
    if source == "FAVCLICK":
        _recent_favclick_ts = time.time()
    # cp_hook 云端源 spool（NE/KG...）若在 FAVCLICK 后短时间内到达 → 是同一点击的云端回声
    # （错歌），drop，避免双链竞速覆盖正确曲。FAVCLICK 自身走正常 dedupe/debounce 点播。
    elif source in _CP_SOURCES and (_recent_favclick_ts > 0 and
                                    time.time() - _recent_favclick_ts < _FAVCLICK_GUARD_S):
        logger.info(f"[CchClick] recv name={name} src={source} action=drop(favclick-guard)",
                    module="CchClick")
        return
    # 【2026-11-23 语音应答误报循环修复】：cp_hook 无法在 WS 层区分触屏点击 vs 云端语音应答，
    #  但语音应答必有 external text。近期有语音输入时 drop cp_hook 事件，避免与 LLM 点歌
    #  双链竞速（"反复切歌"根因）；触屏点击（无近期语音）放行。
    if source in _CP_SOURCES and (_recent_voice_ts > 0 and
                                  time.time() - _recent_voice_ts < _VOICE_GUARD_S):
        logger.info(f"[CchClick] recv name={name} src={source} action=drop(voice-guard)",
                    module="CchClick")
        return
    # FAVCLICK 是用户最新点击意图（spool 无 artist，dedupe 退化 name 单键会误杀同名语音
    # 点歌），跳过 dedupe；云端回声由上方 favclick-guard 挡，不会双链竞速。
    if source == "FAVCLICK":
        reason = ""
    else:
        reason = dedupe(name, str(ev.get("artist") or ""))
    if reason:
        logger.info(f"[CchClick] recv name={name} src={source} action=drop({reason})",
                    module="CchClick")
        return
    _pending_ev = ev
    if _pending_task is not None:
        _pending_task.cancel()
    _pending_task = asyncio.create_task(_debounced_play(ev))
    logger.info(f"[CchClick] recv name={name} src={source} action=hold(debounce={_debounce_s():g}s)",
                module="CchClick")


async def _debounced_play(ev: dict) -> None:
    global _pending_ev, _pending_task
    try:
        await asyncio.sleep(_debounce_s())
    except asyncio.CancelledError:
        return   # 被更新的点击替换（last-tap-wins）
    _pending_ev = None
    _pending_task = None
    async with _play_lock:           # 上一播放链（~35s）未完时串行等待
        await _play_event(ev)


def _debounce_s() -> float:
    try:
        return float(_cfg("cch_click_debounce_s", _DEBOUNCE_DEFAULT_S) or _DEBOUNCE_DEFAULT_S)
    except (TypeError, ValueError):
        return _DEBOUNCE_DEFAULT_S


# 收藏栏标题「歌名-《专辑》-平台」仅作展示清洗（剥出纯歌名用于播报/兜底/占窗）——
# 不再从标题解析平台：平台/rawID 由 native tap_hook 从点击日志源头带进 spool（§2.77-57），
# 用真相源点播，避免「用 UI 显示格式传数据」的脆弱耦合。
_FAV_TITLE_RE = re.compile(r"^(.+)-《[^》]*》-(.+)$")


def clean_fav_name(name: str) -> str:
    """「歌名-《专辑》-平台」→ 纯歌名；非该格式原样返回。仅展示清洗，不承担平台传递。"""
    n = str(name or "").strip()
    m = _FAV_TITLE_RE.match(n)
    return m.group(1).strip() if m else n


async def _play_event(ev: dict) -> None:
    """实际播放：二次去重防线 → 占窗 → 复用语音点歌链（target=cch）→ 单发收尾。"""
    name = str(ev.get("name") or "")
    artist = str(ev.get("artist") or "")
    source = str(ev.get("source") or "")
    raw_id = ""
    platform = ""
    if source == "FAVCLICK":
        # 真相源：native 从 playFavorite 日志带的 item_id(=rawID) + source(=平台)，
        # 写进 spool 的 rid/album 字段。跳过按标题重搜，直接按 rawID+平台点播
        # （平台角标正确、命中收藏原始版本、同名曲靠 rawID 区分）。
        raw_id = str(ev.get("rid") or "").strip()
        platform = str(ev.get("album") or "").strip().lower()
        # name 仅用于播报/兜底：native 不再 ring resolve（哨兵），按 rawID 从
        # favorites.json 真相源反查 display title；查不到保留原值，不影响点播。
        if raw_id and name in ("favclick", "histpos", ""):
            name = await _fav_name_by_rawid(raw_id) or name
        name = clean_fav_name(name)
        if raw_id and platform:
            logger.info(f"[CchClick] FAVCLICK rawID={raw_id} src={platform} "
                        f"name={name!r}", module="CchClick")
        else:
            # 旧 so / 解析异常：无真相源字段，回退按歌名重搜（fail-open，不比修复前差）
            logger.warning("[CchClick] FAVCLICK missing rawID/source, fallback search",
                           module="CchClick")
    # FAVCLICK 是用户最新点击意图（无 artist，dedupe 退化会误杀同名语音点播）——跳过
    # 二次 dedupe，与入口一致；云端回声由 favclick-guard 挡。
    reason = "" if source == "FAVCLICK" else dedupe(name, artist)
    if reason:
        # 去抖窗口内状态变化（如更早的播放刚占窗）——二次防线，宁少播不重播
        logger.info(f"[CchClick] recv name={name} src={source} action=drop({reason})",
                    module="CchClick")
        return
    note_recent_play(name, artist)   # 自身占窗：CP+createlist 双发不二次起播
    logger.info(f"[CchClick] recv name={name} src={source} action=play", module="CchClick")
    from core.tools import song_request

    run_args = {"action": "play", "query": name, "artist": artist, "target": "cch"}
    if raw_id and platform:
        # 源头真相源直传：skip 搜索，song_request 据 rawID+平台直接构造 item 取票据
        run_args["raw_id"] = raw_id
        run_args["source"] = platform
    result = await song_request.run(run_args)
    logger.info(f"[CchClick] play done name={name} result={str(result)[:80]}",
                module="CchClick")
    # 单发收尾（票 23 实测病灶）：点击播 = 单曲意图；置 no_advance 标志防
    # STATE finish → cch_np advance 连播搜索变体（「都是刘巧儿不同片段」）。
    # 不清 items——收藏（票 13 collect_favorite）读当前曲队列；
    # 标志由下一次语音点歌路径显式清除。
    try:
        song_request._cch_state.update({"entry": 0, "no_advance": True})
    except Exception:  # noqa: BLE001
        pass
    asyncio.create_task(_resume_if_paused())


async def run_worker() -> None:
    """常驻轮询循环（APIServer.start 起任务；enabled()=False 不启动）。

    单条事件失败只计数不退出；连续 3 次拉取失败降频 5s（design §2.6）。
    """
    logger.info("[CchClick] worker started (poll=1s)", module="CchClick")
    fail_streak = 0
    delay = _POLL_S
    try:
        while True:
            try:
                from core.music import cch_cast

                ok, payload = await cch_cast.musicctl("clickpoll", {"max": _RECV_MAX})
                events = payload.get("events") if ok and isinstance(payload, dict) else None
                if events is None:
                    raise RuntimeError("clickpoll not ok")
                fail_streak = 0
                delay = _POLL_S
                for ev in events:
                    try:
                        _accept_event(ev)
                    except Exception as exc:  # noqa: BLE001 - 单条失败不影响 worker
                        logger.warning(f"[CchClick] handle err: {type(exc).__name__}",
                                       module="CchClick")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 拉取失败降频续跑
                fail_streak += 1
                if fail_streak >= _FAIL_STREAK_SLOW:
                    delay = _SLOW_POLL_S
                logger.warning(f"[CchClick] poll err ({fail_streak}): {type(exc).__name__}",
                               module="CchClick")
            await asyncio.sleep(delay)
    finally:
        if _pending_task is not None:
            _pending_task.cancel()
            if fail_streak >= _FAIL_STREAK_SLOW:
                delay = _SLOW_POLL_S
            logger.warning(f"[CchClick] poll err ({fail_streak}): {type(exc).__name__}",
                           module="CchClick")
        await asyncio.sleep(delay)
