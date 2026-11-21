"""工具：song_request（语音点歌，票 01 / 票 24 取源重构）。

取源分工（票 24）：
- **首选 Songloft**（音乐服务端）：`core/music/songloft_api.py` 经 `/api/v1/jsplugin/kwqq/*`
  拿聚合搜索（插件按 酷狗→网易云→QQ→酷我→咪咕→千千 交错合并）与跨源歌词（含逐字 `lxlyric`）；
- **兜底门面 kwqq-api**（默认 http://10.10.10.2:3003，内网免 key）：Songloft 不可用时按同一
  源序逐源回落，单曲两跳 `/{source}/search` → `/{source}/song/url`（320k）。

专辑走 `/{source}/album`（API-REFERENCE §4.4：一次调用给候选专辑 + 首选专辑官方曲序），
曲目解析复用同一条 song/url 二段式。

契约事实源：musicdl 仓库 docs/API-REFERENCE.md——业务端点统一信封
`{code,msg,data,timestamp}`，`code != 200` 即业务失败（HTTP 200 不代表成功）；
`ekey` 加密件不进播放层（ADR-0002）；quality 固定 320k（消费铁律 ≤320k mp3）。

分级：RISK = "a"（复述即执行——点歌副作用小、可即时停止，spec 裁决 1）；
选曲确认取最轻形态：播报第一首并复述曲名/歌手，「换一首」由队列 next 支持，
不扩 guardrails 选项式多轮（现框架是/否单槽）。

队列：模块级内存状态（当前曲 + 历史 + 候选集），next/prev 就地切 URL 播放，
stop 走 speaker.stop_device_audio()（bridge 无原生队列，spec 裁决 3）。桥自持
队列为空时 next/prev 回落设备自身队列（能力层 playback_next/playback_prev）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp

from core.ref import get_speaker
from core.utils.config import ConfigManager
from core.utils.logger import logger

NAME = "song_request"

# spec 裁决 1：a = 复述即执行（guardrails A 级一次确认）
RISK = "a"

# 本轮默认播放目标（T2 ticket 08）：规则兜底层。
# 外部文本轮按 ASR 来源设置（cch-screen→cch，LX06 原生→lx06）；轮末 reset。
# 模块级在途上下文——单事件循环串行会话，与 suppress_reply 同构。
_playback_target_context = ""
_playback_target_text = ""


def set_playback_target(target: str, text: str | None = None) -> None:
    """由会话层在轮首设置本轮默认目标（cch/lx06）与原话；仅接受白名单值。

    `text` 用于区分「用户点名了小爱音箱」与「模型被历史带偏」——上下文为 cch 时，
    只有原话含设备关键词才允许显式投 lx06。
    """
    global _playback_target_context, _playback_target_text
    _playback_target_context = target if target in ("cch", "lx06") else ""
    _playback_target_text = str(text or "")


def reset_playback_target() -> None:
    """轮末清除本轮默认目标（仿 suppress_reply 的复位点）。"""
    global _playback_target_context, _playback_target_text
    _playback_target_context = ""
    _playback_target_text = ""


def _resolve_target(explicit) -> str:
    """目标判定（两层结合）：本轮上下文优先于 LLM 显式值，避免模型被历史对话带偏。

    - 上下文 = cch（CCH 屏说话）：只有**本轮原话点名设备**（小爱/音箱/客厅音响）才允许
      投 lx06；否则一律 cch（2026-11-21 实测：模型连续两轮把 CCH 点歌误投 LX06）。
    - 无上下文的唤醒路径：LLM 显式值优先，默认 lx06。
    """
    ctx = _playback_target_context
    if isinstance(explicit, str) and explicit.strip().lower() in ("cch", "lx06"):
        explicit = explicit.strip().lower()
    else:
        explicit = None
    if ctx == "cch":
        if explicit == "lx06":
            if re.search(r"小爱|音箱|客厅音响", _playback_target_text or ""):
                return "lx06"   # 用户本轮原话点名了音箱设备
            logger.info("[SongRequest] target lx06 ignored: no device keyword in utterance; using cch",
                        module="SongRequest")
        return "cch"
    return explicit or ctx or "lx06"

# 消费铁律（spec §0）：播放类消费方只吃 ≤320k mp3
_QUALITY = "320k"

# 单次搜索拉取的候选数（「换一首」在候选集内轮转）
_SEARCH_LIMIT = 5

# 兜底用的门面逐源顺序：与 Songloft kwqq 插件的 SEARCH_SOURCE_ORDER 对齐
# （唯一权威在插件；这里只在 Songloft 不可用时按同一顺序逐源回退，票 24）。
# bilibili 不入表——直链必须随 Referer/UA headers，设备 mediaplayer 无 headers 通道。
_DEFAULT_SOURCES = ("kugou", "netease", "qq", "kuwo", "migu", "qianqian")

# 专辑端点的源策略（API-REFERENCE §4.4，2026-10-28 v1.7）：仅 netease 已接入，
# 其余源返回 HTTP 501（能力缺失）。列表/单曲的源策略与专辑互不影响。
_ALBUM_SOURCES = ("netease",)

# 播放完成回读（要点 4，incident-music-next-silent-drop ③）：发起成功 ≠ 出声。
# URL 源不可达时设备只发 status:1、状态不翻转 ⇒ 轮询超时兜底返回未确认。
_CONFIRM_ATTEMPTS = 5
_CONFIRM_INTERVAL_S = 1.0

# 播报文案降级（要点 8）：歌名/歌手撞 KWS 词表时改用的通用文案。
# 词表交叉检查为子串包含，文案必须避开「点歌/下一首歌曲/停止音乐」等词。
_FALLBACK_ANNOUNCE = "为你播放找到的第一首曲目"

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "按歌名/歌手/专辑名在线搜歌并马上在音箱播放（320k 音质）。"
            "query 可以是歌曲名、歌手名或专辑名（按关键词检索，专辑名会返回该专辑曲目）；"
            "action=play 播放 query 指定的歌；action=album 播放整张专辑"
            "（query=专辑名，artist 可提高同名专辑命中率），按官方曲序连播；"
            "action=next/prev 切上一首下一首（优先桥内点歌队列，无队列时切设备自身队列）；"
            "action=pause 暂停；action=resume 继续/恢复播放（用户说「继续播放」时用这个，"
            "不要用 next）；action=stop 停止。play/album 需要 query。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "album", "next", "prev", "pause", "resume", "stop"],
                    "description": (
                        "play=点歌播放，album=整张专辑连播，next/prev=切换，"
                        "pause=暂停，resume=继续播放，stop=停止"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": "搜索词（歌名/歌手；action=album 时为专辑名）。play/album 必填",
                },
                "artist": {
                    "type": "string",
                    "description": "可选：歌手名（action=album 时用于同名专辑排序）",
                },
                "source": {
                    "type": "string",
                    "description": "可选：指定音源（kuwo/netease/qq），默认按配置顺序",
                },
                "target": {
                    "type": "string",
                    "enum": ["cch", "lx06"],
                    "description": (
                        "可选：播放目标设备。用户明确说「在小爱音箱上播放」时为 lx06；"
                        "「在天猫精灵/屏幕上播放」或未指定时按当前对话来源设备决定"
                        "（CCH 语音默认 cch）。"
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


def _cfg(key: str, default=None):
    """读 song_request 配置段（根 config.py 与 deploy/config.py 双份维护）。"""
    return ConfigManager.instance().get_app_config(f"song_request.{key}", default)


def validate(args: dict) -> str | None:
    """参数校验：action 白名单 + play/album 必带 query（返回拒绝原因即拒绝）。"""
    action = str((args or {}).get("action") or "").strip().lower()
    if action not in ("play", "album", "next", "prev", "pause", "resume", "stop"):
        return "action 必须是 play/album/next/prev/pause/resume/stop"
    if action in ("play", "album") and not str((args or {}).get("query") or "").strip():
        return "play/album 需要 query（歌名、歌手或专辑名）"
    return None


async def run(args: dict) -> str:
    """执行入口（registry 分发；异常收敛为错误串）。

    配置闸门：enabled=false（或缺省）一律拒绝——真机验收窗口由用户打开。
    """
    reason = validate(args)
    if reason:
        return f"已拒绝：{reason}"
    if not bool(_cfg("enabled", False)):
        logger.info("[SongRequest] disabled by config (song_request.enabled=false)")
        return "在线点歌功能未启用（song_request.enabled），请在配置中打开后再用"

    action = str(args.get("action") or "").strip().lower()
    target = _resolve_target(args.get("target"))

    # CCH 语音播控（T2 ticket 08）：目标为 CCH 时走投屏链（Songloft 票据 URL
    # → /cch/cast → dlnaserver → 桥 APK），不经 LX06 的 speaker.play。
    if target == "cch":
        return await _run_cch(action, args)

    if _managed_mode():
        return await _run_managed(action, args)
    if action == "stop":
        return await _do_stop()
    if action == "pause":
        return await _do_pause()
    if action == "resume":
        return await _do_resume()
    if action == "next":
        return await _do_switch(1)
    if action == "prev":
        return await _do_switch(-1)
    if action == "album":
        return await _do_album(
            str(args.get("query") or "").strip(),
            args.get("source"),
            str(args.get("artist") or "").strip(),
        )
    return await _do_play(str(args.get("query") or "").strip(), args.get("source"))


# ---- 托管队列迁移（B-06）：flag 打开后队列归 ManagedStore，旧 _state 不再维护 ----

_host_key_at = 0.0


async def _ensure_host_key() -> None:
    """把 M5 派生 key 部署到设备（10 分钟内复用，省掉每轮的 9102 往返）。"""
    global _host_key_at
    if time.time() - _host_key_at < 600:
        return
    from core.music.cch_cast import host_token_deploy

    ok, _ = await host_token_deploy()
    if ok:
        _host_key_at = time.time()


async def _confirm_stop() -> None:
    try:
        from core.music import cch_cast

        await cch_cast.host_cmd("stop", timeout_s=2.0)
    except Exception:  # noqa: BLE001 - 确认失败不阻塞
        pass


async def _robust_stop() -> None:
    """停当前 M5 会话：广播有间歇丢失（facts §2.64 实测）。

    快路径连发 3 次（约 0.36s，保证「立刻暂停」），带回执的确认丢到后台任务，
    不拖慢后续搜索/取票。
    """
    from core.music import cch_cast

    for _ in range(3):
        try:
            await cch_cast.host_cmd_nowait("stop")
        except Exception:  # noqa: BLE001 - 单次失败不影响重发
            pass
        await asyncio.sleep(0.12)
    asyncio.create_task(_confirm_stop())


def _managed_mode() -> bool:
    from core.music import song_bridge

    return song_bridge.managed_enabled()


async def _songloft_candidates(query: str) -> list[dict]:
    """Songloft 聚合搜索（插件按 SEARCH_SOURCE_ORDER 合并）→ 去重候选。失败抛 SongloftError。"""
    from core.music import songloft_api

    async with aiohttp.ClientSession() as http:
        items = await songloft_api.search(query, page_size=_SEARCH_LIMIT, http=http)
    return _dedupe_items(_drop_query_variants(items, query))


async def _managed_collect(query: str, requested_source, *, artist: str = "",
                           album: bool = False) -> list[dict]:
    """复用既有搜索/专辑候选逻辑，只把「队列归属」换到托管面。

    单曲：**先走 Songloft 聚合搜索**（插件按 酷狗→网易云→QQ→酷我→咪咕→千千 交错合并，
    歌词/平台字段同源），Songloft 不可用时按同一顺序逐源回落门面；专辑仍走门面专辑端点。
    显式指定 source 时直接走门面该源（用户的定向意图优先）。
    """
    if not album and requested_source is None:
        try:
            items = await _songloft_candidates(query)
            if items:
                return items
        except Exception as exc:  # noqa: BLE001 - Songloft 不可用即回落门面（票 24 保留兜底）
            logger.warning(f"[SongRequest] songloft search failed, facade fallback: {type(exc).__name__}",
                           module="SongRequest")

    sources = _sources(requested_source)
    if not sources:
        return []
    async with aiohttp.ClientSession() as session:
        for source in sources:
            if album:
                items = await _album(session, source, query, artist)
            else:
                items = _dedupe_items(
                    _drop_query_variants(await _search(session, source, query), query)
                )
            if items:
                return items
    return []


async def _run_managed(action: str, args: dict) -> str:
    from core.music import song_bridge

    if action in ("stop", "pause", "resume", "next", "prev"):
        result = await song_bridge.command(action)
        if action in ("stop", "resume", "next", "prev"):
            # 单轮语义（与旧 _do_stop 一致）：停止/恢复/切歌都是媒体命令，
            # 完成后结束本轮对话——避免音乐被 VAD 当成说话，或停播后还空等 10–20s。
            from core.tools.registry import suppress_reply

            suppress_reply()
        return result
    query = str(args.get("query") or "").strip()
    items = await _managed_collect(
        query,
        args.get("source"),
        artist=str(args.get("artist") or "").strip(),
        album=(action == "album"),
    )
    if not items:
        return f"没有找到能播放的曲目《{query}》"
    result = await song_bridge.play_items(items, album=(action == "album"))
    from core.tools.registry import suppress_reply

    suppress_reply()   # 音乐已起播：本轮收尾 TTS 不得顶掉它
    return result


# ---- CCH 语音播控（T2 ticket 08）----
# 目标设备 = cch 时：搜歌 → Songloft prepare 拿票据 URL → /cch/cast 投屏。
# 播控（pause/resume/next/stop）经 patched dlnaserver 的 DLNA SOAP；stop 同时撤票。
_CCH_SESSION = "bridge-cch"
_CCH_EPOCH = 1
_cch_state = {"entry": 0, "items": [], "session": _CCH_SESSION}


async def _run_cch(action: str, args: dict) -> str:
    from core.music.cch_cast import (
        build_render_data,
        cast,
        cast_enabled,
        cast_native,
        cast_native_simple,
        host_cmd,
        host_cmd_nowait,
        host_token_deploy,
        kwqq_meta,
        m5_enabled,
        musicctl,
        np_enabled,
        prepare_playback,
        push_queue,
        resolve_ticket_url,
        revoke,
        speak,
        wait_np_play,
    )
    from core.tools.registry import suppress_reply

    np_on = np_enabled()

    # D 方向 kwqq 点播链（票 15/17，2026-11-20）：票据 URL + kwqq 元数据 → renderData
    # → 9102 /cmd COMMAND 7（桥 v0.4.4 内 stop→push→重连→play，原厂内核出声）。
    # 闸门 cch_np_enabled 默认 False，部署窗口由用户打开。
    if np_on:
        if action in ("pause", "resume", "next", "stop"):
            if action == "pause":
                ok, detail = await cast_native_simple(3)
                suppress_reply()
                return "已暂停 CCH 播放" if ok else f"CCH 暂停失败：{detail}"
            if action == "resume":
                ok, detail = await cast_native_simple(2)
                suppress_reply()
                return "已恢复 CCH 播放" if ok else f"CCH 恢复失败：{detail}"
            if action == "next":
                ok, detail = await cast_native_simple(8)
                suppress_reply()
                return "已切换 CCH 曲目" if ok else f"CCH 切歌失败：{detail}"
            # stop：旧票逐条撤（spec 裁决 4「换代旧票逐条 /managed/command stop」，
            # 不依赖 NewEpoch）+ prestop 收尾（Control 22 directStop：停播且清 native
            # 残留队列；18 stop 有 isQuitting 永久 true 副作用弃用，facts §2.48③）
            cur = _cch_state.get("items") or []
            for i, it in enumerate(cur):
                entry_id = str(it.get("id") or f"cch-{i}")
                try:
                    await revoke(_CCH_SESSION, _CCH_EPOCH, entry_id)
                except Exception as exc:  # noqa: BLE001 - 撤票失败不阻塞响应（30min TTL 兜底）
                    logger.warning(f"[SongRequest] cch revoke({entry_id}) err: {type(exc).__name__}")
            await musicctl("prestop")
            _cch_state.update({"entry": 0, "items": []})
            suppress_reply()
            return "已停止 CCH 播放"
        if action == "prev":
            # 桥 v0.4.4 未映射 skipToPrevious（facts §2.36 命令表无 prev 号）
            suppress_reply()
            return "CCH 暂不支持上一首"
        # play / album：搜歌 → songloft prepare → renderData → 9102 /cmd
        query = str(args.get("query") or "").strip()
        if action == "play":
            # 立即暂停（用户 2026-11-21）：派生 key 有 10 分钟缓存；stop 走「连发+回执」，
            # 广播有间歇丢失，单发一次不够（否则旧曲会在播报前继续响）。
            try:
                await _ensure_host_key()
                await _robust_stop()
            except Exception as exc:  # noqa: BLE001 - 早停失败不阻塞后续
                logger.warning(f"[SongRequest] cch early stop failed: {type(exc).__name__}",
                               module="SongRequest")
        items = await _managed_collect(
            query, args.get("source"),
            artist=str(args.get("artist") or "").strip(),
            album=(action == "album"),
        )
        if not items:
            return f"没有找到能播放的曲目《{query}》"
        item = items[0]
        # artist 过滤：kuwo 搜索相关性曾首结果错歌（晴天→风吹麦浪，2026-11-20 实测）
        _artist = str(args.get("artist") or "").strip()
        if _artist:
            for it in items:
                if _artist in str(it.get("artist") or ""):
                    item = it
                    break
        # 票 23：登记本次主动点播（cch_click worker 8s 时间窗去重的事实源）——
        # 云端对同一次点播会回发 ContentPlay/createlist（§2.71 实测），native 无法
        # 区分语音点歌与历史点击，靠这里占窗丢弃回声。占窗失败不阻塞点播。
        try:
            from core.music.cch_click import note_recent_play

            note_recent_play(str(item.get("name") or ""), str(item.get("artist") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[SongRequest] cch note_recent_play err: {type(exc).__name__}",
                           module="SongRequest")
        ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
               "extra": item.get("extra") or {}}
        entry = str(item.get("id") or "cch-1")

        async def _announce_only() -> None:
            """播报并等读完（与取票并行启动，隐藏 ~2s 解析耗时）。"""
            try:
                text = _cch_announce_text(item)
                ok_speak, _ = await speak(text)
                if ok_speak:
                    await asyncio.sleep(_tts_wait_seconds(text))
            except Exception as exc:  # noqa: BLE001 - 播报失败不阻塞起播
                logger.warning(f"[SongRequest] cch announce failed: {type(exc).__name__}",
                               module="SongRequest")

        # 播报与「取元数据/票据/背景图」并行：TTS 是 5s 级，解析是 1–2s，重叠可省时间。
        ann_task = None
        if action == "play" and _cfg("cch_announce_on_play", True):
            ann_task = asyncio.create_task(_announce_only())

        try:
            meta = await kwqq_meta(ref["id"], ref.get("source") or "kuwo")
            # 虚化背景图（与播报并行）：封面 → 服务端高斯模糊 → 缓存 → 公开 URL
            bg_url = ""
            try:
                from core.music import bg_cache
                from core.music.cch_cast import bg_base

                async with aiohttp.ClientSession() as http:
                    bg_url = await bg_cache.ensure(
                        str(meta.get("cover") or item.get("cover") or ""),
                        base=bg_base(), http=http)
            except Exception as exc:  # noqa: BLE001 - 背景失败回退封面
                logger.warning(f"[SongRequest] bg blur failed: {type(exc).__name__}",
                               module="SongRequest")
            if m5_enabled():
                # M5 轨：songloft 票据为主（多源统一门面 + 可撤销 + 代次闸门 +
                # 凭据隔离；poll 404 已加 re-prepare 自愈）。票据链失败/预览 ⇒
                # kwqq 直链 fallback（走 _sources 多源策略 + 预览检测）。
                await _ensure_host_key()
                play_url = None
                try:
                    ticket_url = await resolve_ticket_url(
                        ref, session=_CCH_SESSION, epoch=_CCH_EPOCH, entry=entry)
                    async with aiohttp.ClientSession() as http:
                        # 用 Range GET 而非 HEAD：Songloft /media 的 HEAD 未注册（SPA 兜底成
                        # text/html），HEAD 判定会把全曲票据误杀。只取 1KB 头部即可判类型/总长。
                        async with http.get(
                            ticket_url, headers={"Range": "bytes=0-1023"},
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as hr:
                            ctype = str(hr.headers.get("Content-Type") or "")
                            crange = str(hr.headers.get("Content-Range") or "")
                            clen = int(hr.headers.get("Content-Length") or 0)
                            await hr.read()
                    if _ticket_looks_playable(ctype, crange, clen):
                        play_url = ticket_url
                    else:
                        logger.warning(
                            f"[SongRequest] ticket rejected (type={ctype} range={crange} len={clen});"
                            " fallback direct", module="SongRequest")
                except Exception as exc:  # noqa: BLE001 - 票据链失败降级直链
                    logger.warning(f"[SongRequest] ticket chain failed: {type(exc).__name__}; fallback direct",
                                   module="SongRequest")
                if not play_url:
                    async with aiohttp.ClientSession() as http:
                        for src in _sources(None):
                            play_url = await _pick_url(http, src, item)
                            if play_url:
                                break
                if not play_url:
                    return "CCH 音源暂不可用（kwqq 解析冷却/预览降级），请稍后再试或换音源"
                if ann_task is not None:
                    await ann_task
                # TTS 的 suspend/resume 可能把旧会话唤醒 ⇒ 起播前再补一发 stop（幂等、几十 ms）
                try:
                    await host_cmd_nowait("stop")
                except Exception:  # noqa: BLE001
                    pass
                render_data = build_render_data(play_url, item, meta, background_image=bg_url)
                ok, status = await host_cmd("play", url=play_url)
                if not ok:
                    return f"CCH 点播失败（M5：{status}）"
                # 歌词通道（A3）：Songloft 跨源回搜（优先）→ 门面同源（兜底）→ 设备本地 httpd
                try:
                    lrc = ""
                    async with aiohttp.ClientSession() as http:
                        try:
                            from core.music import songloft_api

                            ly = await songloft_api.lyric(
                                str(item.get("name") or ""),
                                str(item.get("artist") or item.get("singer") or ""),
                                http=http)
                            lrc = songloft_api.pick_lrc(ly)
                        except Exception:  # noqa: BLE001 - Songloft 不可用回落门面同源
                            lr = await _api(http, f"/{ref['source'] or 'kuwo'}/lyric",
                                            {"id": ref["id"]})
                            lrc = str((lr or {}).get("lyric") or "") if isinstance(lr, dict) else ""
                    if lrc:
                        lok, _ = await musicctl("lyric", {"id": ref["id"], "lrc": lrc})
                        if lok:
                            lurl = f"http://127.0.0.1:18081/{ref['id']}.lrc"
                            render_data["lyricUrl"] = lurl
                            ext = json.loads(render_data.get("audioExt") or "{}")
                            ext["lyric"] = lurl
                            render_data["audioExt"] = json.dumps(ext, ensure_ascii=False)
                except Exception:  # noqa: BLE001 - 歌词失败不阻塞播放
                    pass
                # 显示补投（A1 路径）：纯 VIEW intent renderData，不带播控副作用
                await musicctl("render", {"renderData": json.dumps(
                    render_data, ensure_ascii=False)})
            else:
                # 活实例轨：不 prestop（prestop 杀实例 ⇒ cast 死实例无声，§2.49）；
                # 事后验证 cast_need_m5（8s 保守值，P95 基线待采）
                ticket_url = await resolve_ticket_url(
                    ref, session=_CCH_SESSION, epoch=_CCH_EPOCH, entry=entry)
                render_data = build_render_data(ticket_url, item, meta, background_image=bg_url)
                if ann_task is not None:
                    await ann_task
                ok, detail = await cast_native(render_data)
                if ok and not await wait_np_play(8.0):
                    return "CCH 点播未起播（NEED_M5：死实例，待 M5 刷机）"
                if not ok:
                    return f"CCH 点播失败（9102 /cmd：{detail}）"
        except Exception as exc:  # noqa: BLE001 - 收敛为错误串（工具层不抛）
            logger.error(f"[SongRequest] cch native cast failed: {type(exc).__name__}: {exc}")
            return f"CCH 点播失败：{type(exc).__name__}"
        # 存整队供 cch_np 换代编排（STATE finish → advance 下一首）；票据按 entry 逐条 prepare
        # no_advance=False：清除点击播的单发标志（票 23），恢复语音队列的正常换代
        _cch_state.update({"entry": 0, "items": items, "no_advance": False})
        # 票 21：UI 队列下发（搜索结果 topN 元数据，脱敏无票据）。失败不阻塞起播。
        try:
            await push_queue(items)
        except Exception as exc:  # noqa: BLE001 - 队列是体验增强
            logger.warning(f"[SongRequest] cch queue push failed: {type(exc).__name__}",
                           module="SongRequest")
        suppress_reply()   # kwqq 源已起播：本轮收尾 TTS 不得顶掉它
        name = str(render_data.get("audioName") or "歌曲")
        return f"已在 CCH 上为你播放《{name}》（队列 {len(items)} 首）"

    # CCH 主动投送默认关闭（票 16/17 的 D 方向裁决）：原厂 = 唯一播放器，bridge = 控制/状态端。
    # 关闭时**不出声、不投送、也不谎报动作**——原厂云链已经在处理这条语音请求，
    # 我方任何 TTS 回复都是干扰，故显式 suppress_reply()。
    # 依据：facts §2.27（双链并发、桥 8/8 必输）+ §2.30（桥是独立第二声源，暂停后仍出声）。
    if not cast_enabled():
        logger.info(
            f"[SongRequest] cch cast disabled (enabled=False) — 原厂链路自行处理 action={action}"
        )
        suppress_reply()
        return ""

    if action in ("pause", "resume", "next", "prev", "stop"):
        # CCH 播控经 DLNA RenderingControl/AVTransport（patched dlnaserver）；
        # 本阶段先给出确定性响应，真机接线在 deploy 层做（见 ticket 08 依赖）。
        if action == "stop":
            cur = _cch_state.get("items") or []
            entry = cur[_cch_state["entry"]].get("id", "") if cur else ""
            try:
                await revoke(_CCH_SESSION, _CCH_EPOCH, entry)
            except Exception as exc:  # noqa: BLE001 - 撤票失败不阻塞响应
                logger.warning(f"[SongRequest] cch revoke err: {type(exc).__name__}")
            _cch_state.update({"entry": 0, "items": []})
            return "已停止 CCH 播放"
        if action in ("next", "prev"):
            items = _cch_state.get("items") or []
            if items:
                _cch_state["entry"] = max(0, min(len(items) - 1,
                                                 _cch_state["entry"] + (1 if action == "next" else -1)))
                suppress_reply()
                return "已切换 CCH 曲目"
            return "CCH 无播放队列"
        suppress_reply()
        return "已暂停 CCH 播放" if action == "pause" else "已恢复 CCH 播放"

    # play / album：搜歌 → Songloft prepare → /cch/cast
    query = str(args.get("query") or "").strip()
    items = await _managed_collect(
        query, args.get("source"),
        artist=str(args.get("artist") or "").strip(),
        album=(action == "album"),
    )
    if not items:
        return f"没有找到能播放的曲目《{query}》"
    item = items[0]
    ref = {"source": item.get("source") or "", "id": str(item.get("id") or ""),
           "extra": item.get("extra") or {}}
    entry = str(item.get("id") or "cch-1")
    try:
        ticket_url = await resolve_ticket_url(
            ref, session=_CCH_SESSION, epoch=_CCH_EPOCH, entry=entry)
        ok = await cast(ticket_url)
    except Exception as exc:  # noqa: BLE001 - 收敛为错误串（工具层不抛）
        logger.error(f"[SongRequest] cch cast failed: {type(exc).__name__}: {exc}")
        return f"CCH 投屏失败：{type(exc).__name__}"
    if not ok:
        return "CCH 投屏失败（/cch/cast 拒绝，检查 CAST_ALLOW 与 PANEL_TOKEN）"
    _cch_state.update({"entry": 0, "items": items[:1]})
    suppress_reply()
    name = str(item.get("name") or "歌曲")
    return f"已在 CCH 上为你播放《{name}》"


# ---- HTTP 两跳（kw-qq-api envelope 契约，事实源 musicdl API-REFERENCE §3/§4） ----


async def _fetch(session: aiohttp.ClientSession, url: str, params: dict):
    """传输层：GET 并解析 JSON body；HTTP 非 200 / 网络 / 解码失败返回 None。"""
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                logger.error(f"[SongRequest] HTTP {resp.status} GET {url}")
                return None
            return await resp.json()
    except Exception as exc:  # noqa: BLE001 - 传输异常收敛（weather 模式）
        logger.error(f"[SongRequest] GET {url} failed: {type(exc).__name__}: {exc}")
        return None


async def _api(session: aiohttp.ClientSession, path: str, params: dict):
    """业务层：envelope 契约（`code != 200` 即业务失败，HTTP 200 不代表成功）。

    返回 data 载荷；传输失败与业务失败统一返回 None——逐源回落策略对
    两者一视同仁。
    """
    base = str(_cfg("api_base", "http://10.10.10.2:3003")).rstrip("/")
    body = await _fetch(session, f"{base}{path}", params)
    if not isinstance(body, dict):
        if body is not None:
            # 非预期 envelope shape（HTTP 200 但不是信封）：真意外，保留 error
            logger.error(f"[SongRequest] unexpected envelope shape GET {path}: {body!r}")
        return None  # 传输失败（None）：_fetch 已记 error，不重复刷屏
    if body.get("code") != 200:
        # 预期内业务失败（搜不到歌 code:404、无直链等）：逐源回落正常路径，降 info
        logger.info(f"[SongRequest] business failure GET {path}: {body}")
        return None
    return body.get("data")
    return body.get("data")


async def _search(session, source: str, query: str) -> list[dict]:
    data = await _api(
        session, f"/{source}/search", {"keywords": query, "limit": _SEARCH_LIMIT}
    )
    # 实测（2026-10-27 真机）：search 的 data 是 {keywords,page,total,items}，
    # 不是裸列表；对旧形状保留兼容（契约文档未写明 data 外形）。
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


async def _album(session, source: str, name: str, artist: str):
    """专辑检索（API-REFERENCE §4.4）：返回 (albums, items)，items 按官方曲序。

    items 与 search 结果同构（另带 track_no）⇒ 曲目直链照走 /song/url。
    未接入该端点的源返回 HTTP 501（能力缺失），由传输层折为 None 供逐源回落。
    """
    params = {"name": name}
    if artist:
        params["artist"] = artist
    data = await _api(session, f"/{source}/album", params)
    if not isinstance(data, dict):
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    items = [item for item in items if isinstance(item, dict)]
    if not items:
        return None
    if all(isinstance(item.get("track_no"), int) for item in items):
        items.sort(key=lambda item: item["track_no"])
    albums = data.get("albums")
    return (albums if isinstance(albums, list) else []), items


def _url_params(source: str, item: dict) -> dict:
    """song/url 请求参数：id + 固定 320k；migu 回传 copyrightId（契约 §4.2）。"""
    params = {"id": str(item.get("id") or ""), "quality": _QUALITY}
    if source == "migu":
        copyright_id = ((item.get("extra") or {}).get("copyrightId")) or ""
        if copyright_id:
            params["copyright"] = str(copyright_id)
    return params


async def _pick_url(session, source: str, item: dict) -> str | None:
    """取可播直链；ekey 加密件视为无直链（ADR-0002：不进播放层）。

    headers 字段刻意忽略——设备播放通道无自定义 headers（bilibili 已排除）。
    """
    data = await _api(session, f"/{source}/song/url", _url_params(source, item))
    if not isinstance(data, dict):
        return None
    url = str(data.get("url") or "").strip()
    if not url:
        return None
    if data.get("ekey"):
        logger.warning(
            f"[SongRequest] {source} item {item.get('id')} returned ekey "
            "(encrypted container); skipped per ADR-0002"
        )
        return None
    # 预览检测：kuwo 匿名风控间歇降级试听链（2026-11-20 实测 30s/120KB）；
    # 全曲 mp3 320k 通常 >1MB / duration>60s，低于阈值视为预览弃用换源。
    dur = data.get("duration_s") or 0
    size = data.get("size_bytes") or 0
    if (dur and dur < 60) or (size and size < 500000):
        logger.warning(
            f"[SongRequest] {source} item {item.get('id')} preview-like "
            f"(dur={dur}s size={size}); skipped"
        )
        return None
    return url


# ---- 源策略（要点 6） ----


def _sources(requested) -> list[str]:
    """按序源列表：缺省 = 插件同序的 `_DEFAULT_SOURCES`（单一真相，不再读旧 `sources` 配置）。"""
    if requested:
        src = str(requested).strip().lower()
        if src == "bilibili" or src not in _DEFAULT_SOURCES:
            return []
        return [src]
    return list(_DEFAULT_SOURCES)


def _album_sources(requested) -> list[str]:
    """专辑源列表：只含已接入 album 端点的源（其余源返回 501）。"""
    configured = _cfg("album_sources") or list(_ALBUM_SOURCES)
    configured = [str(s).strip().lower() for s in configured if s]
    if requested:
        src = str(requested).strip().lower()
        return [src] if src in configured else []
    return configured


# ---- 播放与回读（要点 4） ----


def _announce_text(item: dict) -> str:
    """选曲播报文案（复述曲名/歌手，最轻确认形态）。"""
    name = str(item.get("name") or "").strip() or "未知名曲目"
    singer = str(item.get("singer") or "").strip()
    text = f"为你找到《{name}》"
    if singer:
        text += f"，{singer}"
    return text + "，这就为你播放"


# 门面/插件 source → 人话平台名（票 22 播报用；未知源不硬编）
_SOURCE_LABEL = {
    "kugou": "酷狗",
    "netease": "网易云",
    "kuwo": "酷我",
    "qq": "QQ音乐",
    "migu": "咪咕",
    "qianqian": "千千",
}


_TICKET_MIN_BYTES = 500000


def _ticket_looks_playable(content_type: str, content_range: str, content_length: int) -> bool:
    """票据可用判定：音频类型 + 全曲大小 ≥ 500KB（预览降级判据）。

    `content_range`（`bytes 0-1023/9902692`）里的总长优先于 Content-Length（后者是本次
    区间的长度）。旧实现用 HEAD：Songloft `/media` 未注册 HEAD 路由，会被 SPA 兜底成
    `text/html` 200 ⇒ 全曲票据被误判为预览而丢弃（2026-11-21 事故）。
    """
    ctype = str(content_type or "").lower()
    if "audio" not in ctype:
        return False
    total = 0
    cr = str(content_range or "").strip()
    if "/" in cr:
        try:
            total = int(cr.rsplit("/", 1)[1])
        except ValueError:
            total = 0
    if total <= 0:
        total = int(content_length or 0)
    return total >= _TICKET_MIN_BYTES


def _tts_wait_seconds(text: str) -> float:
    """按字数估算云端 TTS 播完所需时长（facts §2.20：10 字≈2.17s / 32 字≈6.94s，约 0.217s/字）。

    `/speak` 只保证「已下发」，不等待出声；若立即起播，新歌会盖在播报上（2026-11-21 用户反馈
    「先放一点新歌，才播语音提示」）。估算 + 20% 余量，夹在 [1.2, 12] 秒。
    """
    n = len(str(text or "").strip())
    if n <= 0:
        return 0.0
    return max(1.0, min(10.0, 0.20 * n + 0.5))


def _cch_announce_text(item: dict) -> str:
    """CCH 点歌播报：「好的，现在为您播放{歌手}的{曲名}，来自于{平台}。」（票 22 用户口径）"""
    name = str(item.get("name") or "").strip() or "这首"
    singer = str(item.get("artist") or item.get("singer") or "").strip()
    label = _SOURCE_LABEL.get(str(item.get("source") or "").strip().lower(), "")
    text = "好的，现在为您播放"
    if singer:
        text += f"{singer}的"
    text += name
    if label:
        text += f"，来自于{label}"
    return text + "。"


async def _speak_and_start(speaker, message: str, url: str) -> tuple[bool, int]:
    """经 announce 双闸门播报选曲文案后起播 URL（非阻塞）。

    文案撞 KWS 词表（歌名/歌手名不可控）时降级为通用文案重试；
    仍被拒则不打播报、仅起播（announce 拒绝的是文案，不是播放本身）。

    返回 ``(ok, play_seq)``：``play_seq`` 在**起播 URL 之前**取，选曲播报自己的
    playing/idle 事件因此落在下界之外，不会被当成这首曲的起播/结束（B-03）。
    """
    from core.device import get_playback_events
    from core.services.speaker import AnnounceRejected

    for text in (message, _FALLBACK_ANNOUNCE):
        try:
            async with speaker.announce(text=text, reason="song_request", wait=False):
                await speaker.play(text=text)
                seq = get_playback_events().begin_playback()
                ok = bool(await speaker.play(url=url, blocking=False))
                return ok, seq
        except AnnounceRejected as exc:
            logger.warning(f"[SongRequest] announce rejected (word={exc.word}); fallback")
            continue
    seq = get_playback_events().begin_playback()
    return bool(await speaker.play(url=url, blocking=False)), seq


async def _confirm_playing(speaker) -> bool:
    """播放发起后的完成回读：轮询播放状态确认「真的出声了」。

    URL 源不可达时设备只发 status:1、状态不翻转 ⇒ 超时兜底返回 False
    （不无限等待，incident-music-next-silent-drop 教训③）。
    """
    for attempt in range(_CONFIRM_ATTEMPTS):
        await asyncio.sleep(_CONFIRM_INTERVAL_S)
        try:
            state = await speaker.get_playing(sync=True)
        except Exception as exc:  # noqa: BLE001 - 单次回读失败消耗一轮计数，下一轮重试
            logger.warning(f"[SongRequest] playback readback failed: {exc}")
            continue
        if state == "playing":
            if attempt:
                logger.info(f"[SongRequest] playing confirmed after {attempt + 1} polls")
            return True
    return False


# ---- 候选去重（同一首的不同版本不占队列位） ----

# 版本标记：命中即从标题里剥掉，避免「晴天 / 晴天(Live) / 晴天(深情版)」占多个队列位
_VARIANT_MARKERS = ("live", "伴奏", "remix", "cover", "翻自", "翻唱", "demo")
# 标题/歌手归一化：去括号备注（全角/半角）与标点空白
_BRACKET_RE = re.compile(r"[\(（\[【].*?[\)）\]】]")
_PUNCT_RE = re.compile(r"[\s\-_·.,，。!！?？'\"、]+")


def _norm_title(name) -> str:
    text = _BRACKET_RE.sub("", str(name or "").strip().lower())
    for marker in _VARIANT_MARKERS:
        text = text.replace(marker, "")
    return _PUNCT_RE.sub("", text)


def _norm_singer(singer) -> str:
    return _PUNCT_RE.sub("", str(singer or "").strip().lower())


def _primary_singer(singer) -> str:
    """主歌手：专辑曲目的 singer 可能是「甲, 乙」合唱歌手，只取第一位比对。"""
    text = str(singer or "")
    for sep in (",", "，", "/", "&"):
        text = text.split(sep)[0]
    return _norm_singer(text)


def _item_key(item: dict) -> tuple[str, str]:
    # 队列位按「归一化歌名」去重：同一首的不同版本/翻唱都算一首
    # （真机 2026-10-27：按歌名+歌手去重后「下一首」仍在放同一首歌的别的版本）
    return (_norm_title(item.get("name")), "")


def _dedupe_items(items: list[dict]) -> list[dict]:
    """按归一化歌名去重：保留首现顺序（同曲多版本只留一条）。"""
    seen: set[tuple[str, str]] = set()
    kept: list[dict] = []
    for item in items:
        key = _item_key(item)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def _drop_query_variants(items: list[dict], query: str) -> list[dict]:
    """把「检索词 + 额外内容」的长标题当作同曲变体丢掉（保留首个最佳匹配）。

    只在按歌名点播时生效（query 归一化后作为前缀）：真机 2026-10-27 出现过
    「晴天+告白气球 (Live)」这类拼歌/衍生标题占队列位。
    按歌手/专辑检索时标题不以 query 开头，不受影响。
    """
    key = _norm_title(query)
    if not key:
        return items
    kept: list[dict] = []
    for index, item in enumerate(items):
        title = _norm_title(item.get("name"))
        if index and title != key and title.startswith(key):
            continue
        kept.append(item)
    return kept


def _queue_for(current: dict, candidates: list[dict]) -> list[dict]:
    """以「实际播放曲」为基准构建队列：当前曲的衍生/拼接标题不进队列。

    只看检索词不够——LLM 可能传「周杰伦 晴天」，而拼接曲「晴天+告白气球」
    不以该检索词开头（真机 2026-10-27：下一首仍播同曲拼接版）。
    """
    key = _norm_title(current.get("name"))
    queue = [current]
    seen = {key}
    for item in candidates:
        title = _norm_title(item.get("name"))
        if not title or title in seen:
            continue
        if key and title.startswith(key):
            continue
        seen.add(title)
        queue.append(item)
    return queue


async def _extend_with_singer(session, source: str, items: list[dict]) -> list[dict]:
    """候选只有同一首歌时，用歌手再搜一轮补出「别的歌」（队列才有意义）。"""
    singer = str(items[0].get("singer") or "").strip() if items else ""
    if not singer:
        return items
    current_key = _norm_title(items[0].get("name")) if items else ""
    extra = _dedupe_items(await _search(session, source, singer))
    if not extra:
        return items
    seen = {_item_key(i) for i in items}
    merged = list(items)
    for item in extra:
        # 歌手检索结果里仍可能带当前曲的拼歌/衍生标题（如「晴天+告白气球 (Live)」），
        # 按当前曲名再过滤一次（真机 2026-10-27：下一首又播同一首的拼接版）
        title = _norm_title(item.get("name"))
        if current_key and title != current_key and title.startswith(current_key):
            continue
        key = _item_key(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


# ---- play 主路径 ----


async def _try_play_group(speaker, session, raw_items: list[dict], items: list[dict],
                          label: str) -> str | None:
    """对一组候选逐个尝试起播；成功返回回执文案，全失败返回 None。"""
    for item in raw_items:
        ok, confirmed, _played, play_seq = await _start_item(
            speaker, session, item, _announce_text(item)
        )
        if not ok:
            continue  # 无直链/加密件/设备拒收：候选内继续，不行再落下一源
        queue = _queue_for(item, items)
        if len(queue) < 2:
            # 队列只剩同一首：按歌手补别的歌后再按当前曲过滤
            extra = await _extend_with_singer(
                session, str(item.get("source") or label).strip().lower() or label, items)
            queue = _queue_for(item, list(items) + extra)
        _note_played(item, queue, play_seq=play_seq)
        logger.info(
            f"[SongRequest] playing {item.get('name')!r} via {label} "
            f"(confirmed={confirmed})"
        )
        # 工具已自行播报并起播：本轮收尾 TTS 会打断音乐，置免播
        from core.tools.registry import suppress_reply

        suppress_reply()
        suffix = (
            "，已确认出声" if confirmed
            else "，未确认出声（音源可能不可达，可让我停止）"
        )
        return f"已开始播放《{item.get('name')}》-{item.get('singer')}（音源 {label}）{suffix}"
    return None


async def _do_play(query: str, requested_source) -> str:
    if not query:
        return "已拒绝：play 需要 query（歌名或歌手）"
    sources = _sources(requested_source)
    if not sources:
        return f"音源 {requested_source} 不可用（未启用或被排除）"

    speaker = get_speaker()
    if speaker is None:
        logger.error("[SongRequest] speaker not ready")
        return "音箱服务还没就绪，稍后再试"

    async with aiohttp.ClientSession() as session:
        # 票 24：Songloft 聚合搜索优先（未指定 source 时），失败回落门面逐源
        if requested_source is None:
            try:
                candidates = await _songloft_candidates(query)
            except Exception as exc:  # noqa: BLE001 - 回落门面（保留兜底）
                logger.warning(
                    f"[SongRequest] songloft search failed, facade fallback: {type(exc).__name__}",
                    module="SongRequest")
                candidates = []
            if candidates:
                done = await _try_play_group(
                    speaker, session, candidates, candidates,
                    str(candidates[0].get("source") or "songloft"))
                if done:
                    return done
        for source in sources:
            raw_items = _drop_query_variants(await _search(session, source, query), query)
            items = _dedupe_items(raw_items)
            if not items:
                continue
            done = await _try_play_group(speaker, session, raw_items, items, source)
            if done:
                return done

    logger.info(f"[SongRequest] no playable result for {query!r} in {sources}")
    return f"没有找到能播放的《{query}》，换个说法或换个源试试"


def _album_name(items: list[dict], albums: list, query: str) -> str:
    """播报用的专辑名：以实际播放曲目的 album 字段为准（候选列表可能同名异手）。"""
    if items:
        name = str(items[0].get("album") or "").strip()
        if name:
            return name
    for album in albums or []:
        if isinstance(album, dict):
            name = str(album.get("name") or "").strip()
            if name:
                return name
    return query


def _album_announce_text(album_name: str, item: dict) -> str:
    track = str(item.get("name") or "").strip() or "未知名曲目"
    return f"为你播放专辑《{album_name}》，从《{track}》开始"


# 专辑名与检索词的匹配门槛：ASR 把专辑名听错时，/album 会返回一张毫不相干的
# 专辑并直接开播（真机 2026-10-28：「蛋黄涅…小情歌」放出了 29 首的日文 OST）。
_ALBUM_MATCH_RATIO = 0.5


def _album_matches(query: str, album_name: str) -> bool:
    """检索词与返回专辑名是否足够像（防「报错名字却放别的专辑」）。"""
    q = _PUNCT_RE.sub("", _BRACKET_RE.sub("", str(query or "").lower()))
    a = _PUNCT_RE.sub("", _BRACKET_RE.sub("", str(album_name or "").lower()))
    if not q or not a:
        return False
    if q in a or a in q:
        return True
    overlap = len(set(a) & set(q))
    return overlap / len(set(a)) >= _ALBUM_MATCH_RATIO


async def _do_album(query: str, requested_source, artist: str) -> str:
    """专辑连播：一次 /album 调用拿官方曲序，队列按 track_no 顺序向后铺。"""
    sources = _album_sources(requested_source)
    if not sources:
        supported = ", ".join(_album_sources(None))
        return f"音源 {requested_source} 不支持专辑检索（目前仅 {supported}）"

    speaker = get_speaker()
    if speaker is None:
        logger.error("[SongRequest] speaker not ready")
        return "音箱服务还没就绪，稍后再试"

    async with aiohttp.ClientSession() as session:
        for source in sources:
            found = await _album(session, source, query, artist)
            if found is None:
                continue
            albums, items = found
            album_name = _album_name(items, albums, query)
            if not _album_matches(query, album_name):
                # 检索词与命中专辑对不上（多为 ASR 听错）：不播，回报候选专辑
                names = [
                    str(a.get("name") or "").strip()
                    for a in albums
                    if isinstance(a, dict) and str(a.get("name") or "").strip()
                ][:3]
                logger.info(
                    f"[SongRequest] album mismatch: query={query!r} "
                    f"best={album_name!r} candidates={names}"
                )
                hint = "、".join(f"《{n}》" for n in names)
                tail = f"，候选有{hint}" if hint else ""
                return f"没找到与《{query}》匹配的专辑{tail}，换个说法或加上歌手名试试"
            # 逐曲尝试：某首取不到直链就顺延到下一首，队列仍按官方曲序
            for position, item in enumerate(items):
                ok, confirmed, played, play_seq = await _start_item(
                    speaker,
                    session,
                    item,
                    _album_announce_text(album_name, item),
                    search_fallback=True,
                )
                if not ok:
                    continue
                if played is not item:
                    # 跨源补播的曲目补上专辑曲序，队列/日志口径与官方曲序一致
                    played = dict(played, track_no=item.get("track_no"))
                _note_played(played, items[position + 1:], album_mode=True, play_seq=play_seq)
                from core.tools.registry import suppress_reply

                suppress_reply()
                track_no = item.get("track_no")
                label = f"第 {track_no} 首" if isinstance(track_no, int) else "首曲"
                suffix = (
                    "，已确认出声" if confirmed
                    else "，未确认出声（音源可能不可达，可让我停止）"
                )
                logger.info(
                    f"[SongRequest] album {album_name!r} track {track_no} "
                    f"via {played.get('source') or source} (confirmed={confirmed})"
                )
                return (
                    f"已开始播放专辑《{album_name}》{label}"
                    f"《{played.get('name')}》-{played.get('singer')}"
                    f"（音源 {played.get('source') or source}）{suffix}"
                )

    logger.info(f"[SongRequest] no playable album for {query!r} in {sources}")
    return f"没有找到能播放的专辑《{query}》，换个专辑名或指定歌手试试"


def _note_played(item: dict, candidates: list[dict], album_mode: bool = False,
                 play_seq: int = 0) -> None:
    """落队列状态：当前曲 + 历史 + 候选集（同一首只占一个队列位）。

    ``play_seq`` 是本曲起播前的事件序号（`begin_playback()`），完成监听只认
    它之后的事件；0 表示不设下界。
    """
    global _generation, _paused_for_wakeup
    _generation += 1
    _paused_for_wakeup = False
    cancel_pending_resume()  # 新曲已起播：旧的自动续播作废
    state = _state
    if state["current"] is not None:
        state["history"].append(state["current"])
        del state["history"][:-16]  # 防涨：prev 只回看最近若干首
    key = _norm_title(item.get("name"))
    queue = [item] + [c for c in candidates if _norm_title(c.get("name")) != key]
    state["current"] = item
    state["candidates"] = queue
    state["index"] = 0
    state["album"] = album_mode
    state["started_at"] = time.monotonic()
    state["play_seq"] = int(play_seq or 0)   # 本曲起播前的事件序号（完成监听的下界）
    # 专辑队列：挂上「播完自动下一首」监听；点歌/普通播放则清掉旧监听
    _cancel_advance()
    if album_mode:
        _arm_advance()


# ---- 队列动作（要点 5：内存队列，next/prev 就地切 URL，stop 走设备停播） ----


def _switch_text(item: dict, direction: str) -> str:
    """切歌播报文案（注意避开 KWS 词表词：不说「下一首歌曲/上一首歌曲」）。"""
    name = str(item.get("name") or "").strip() or "未知名曲目"
    singer = str(item.get("singer") or "").strip()
    prefix = "接下来是" if direction == "next" else "回到刚才那首"
    text = f"{prefix}《{name}》"
    if singer:
        text += f"，{singer}"
    return text


def has_queue() -> bool:
    """是否有在线点歌队列（原生唤醒的队列控制词路由用，见 config before_wakeup）。"""
    return _state.get("current") is not None


def queue_health() -> dict:
    """队列运行态（`/api/health` 观测用；只给计数/标志，不给歌名）。"""
    current = _state.get("current")
    return {
        "has_queue": current is not None,
        "album": bool(_state.get("album")),
        "index": int(_state.get("index", -1)),
        "size": len(_state.get("candidates") or []),
        "paused_for_wakeup": bool(_paused_for_wakeup),
        "auto_advance": _advance_task is not None and not _advance_task.done(),
    }


# ---- 唤醒中断：暂停而不是停播（有队列时），处理完自动续播 ----
# 真机 2026-10-27：原生「小爱同学」唤醒走 wakeup_session.on_interrupt，原策略
# 是 stop_device_audio（停），导致「调低音量」这类命令必须牺牲当前曲目。
# 这里改为 pause + 空闲后 auto-resume；新点歌（_note_played）会让旧续播任务作废。
_RESUME_POLL_S = 1.0
_RESUME_IDLE_STREAK = 2  # 连续空闲 2 秒才续播，避开自己的 TTS 尾巴
_RESUME_MAX_S = 30.0

_resume_task = None
_advance_task = None
# 切歌串行化：自动续播与手动「下一首」可能同时到这里（真机 2026-10-28 撞车
# 导致同一首被起播两次、队列下标错乱）
_switch_lock = asyncio.Lock()
_generation = 0
_paused_for_wakeup = False


def is_paused_for_wakeup() -> bool:
    """当前暂停是否由唤醒打断引起（用于避免重复 stop 把暂停变成停播）。"""
    return _paused_for_wakeup


def cancel_pending_resume() -> None:
    """作废待续播任务（新曲已起播 / 会话重置时调用）。"""
    global _resume_task
    if _resume_task is not None and not _resume_task.done():
        _resume_task.cancel()
    _resume_task = None


async def pause_for_wakeup() -> bool:
    """唤醒打断：在线点歌在播时暂停并排 program 自动续播；返回是否已接管。"""
    global _resume_task, _paused_for_wakeup
    if not has_queue():
        return False
    if _paused_for_wakeup:
        return True  # 已是唤醒暂停态：幂等，避免被后续 stop 覆盖
    speaker = get_speaker()
    if speaker is None:
        return False
    try:
        if await speaker.get_playing(sync=True) != "playing":
            return False
        if not await speaker.set_playing(False):
            return False
    except Exception as exc:  # noqa: BLE001 - 接管失败由调用方回落停播
        logger.warning(f"[SongRequest] pause for wakeup failed: {exc}")
        return False
    _paused_for_wakeup = True
    generation = _generation
    cancel_pending_resume()
    _resume_task = asyncio.ensure_future(_auto_resume(generation))
    logger.info("[SongRequest] paused for wakeup; auto-resume armed")
    return True


async def resume_after_wakeup(reason: str = "command") -> bool:
    """续播（若仍持有同一首）。幂等：已在播则直接 True。

    只续播「唤醒打断」造成的暂停：用户自己说「暂停」后，会话结束不得越权恢复。
    """
    global _paused_for_wakeup
    cancel_pending_resume()
    if not _paused_for_wakeup:
        return False
    if not has_queue():
        return False
    speaker = get_speaker()
    if speaker is None:
        return False
    try:
        if await speaker.get_playing(sync=True) == "playing":
            _paused_for_wakeup = False
            return True
        ok = bool(await speaker.set_playing(True))
        if ok:
            _paused_for_wakeup = False
        logger.info(f"[SongRequest] resumed after wakeup (reason={reason}, ok={ok})")
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SongRequest] resume after wakeup failed: {exc}")
        return False


async def _auto_resume(generation: int) -> None:
    """等待设备回到空闲（原生 TTS 播完）后自动续播；有新曲则作废。"""
    global _paused_for_wakeup
    speaker = get_speaker()
    if speaker is None:
        # 拿不到 speaker 就永远续不了播：释放暂停态，别让
        # pause_for_wakeup 的幂等闸门一直谎报「已接管」。
        _paused_for_wakeup = False
        return
    waited = 0.0
    idle_streak = 0
    while waited < _RESUME_MAX_S:
        await asyncio.sleep(_RESUME_POLL_S)
        waited += _RESUME_POLL_S
        if generation != _generation:
            return
        try:
            state = await speaker.get_playing(sync=True)
        except Exception:
            continue
        if state == "playing":
            idle_streak = 0
            continue
        idle_streak += 1
        if idle_streak >= _RESUME_IDLE_STREAK:
            await resume_after_wakeup("auto")
            return
    # 超时窗口耗尽（设备一直不在空闲：被外部长播占住 / 一直有人说话）：
    # 本任务的续播职责到此为止，释放暂停态——否则下一轮唤醒会被
    # is_paused_for_wakeup 短路成「不暂停不接管」，播放中唤醒的暂停语义失效。
    if generation == _generation:
        _paused_for_wakeup = False
        logger.info("[SongRequest] auto-resume window expired; paused flag released")


async def _do_stop() -> str:
    """停止播放：设备侧全链路停 + 清队列（设备可能还在放其它来源，也要能停）。"""
    global _paused_for_wakeup, _generation
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    await speaker.stop_device_audio()
    # v0.4.6：CCH M5 播放也停——hostcmd stop = 停借句柄会话 + 清宿主 self-heal 状态
    # （「退下/停止播放」不再被 self-heal 拉回，2026-11-21 真机对抗实测）
    try:
        from core.music.cch_cast import host_cmd
        await host_cmd("stop")
    except Exception as exc:
        logger.warning(f"[SongRequest] CCH hostcmd stop failed: {exc}")
    cancel_pending_resume()
    _cancel_advance()
    _generation += 1  # 作废正在等待播完的续播监听
    _paused_for_wakeup = False
    _state.update(
        {
            "current": None,
            "candidates": [],
            "index": -1,
            "history": [],
            "album": False,
            "started_at": 0.0,
            "play_seq": 0,
        }
    )
    logger.info("[SongRequest] stopped and queue cleared")
    from core.tools.registry import suppress_reply

    suppress_reply()  # 音乐已停：不再插一句确认播报，本轮直接退出会话
    return "已停止播放，队列已清空"


async def _do_pause() -> str:
    """显式暂停（用户说「暂停」）：保留队列，不挂续播；与唤醒暂停态区分。"""
    global _paused_for_wakeup
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    cancel_pending_resume()
    _cancel_advance()
    # 用户主动暂停：清掉「唤醒暂停」标志——否则会话结束时的 resume_after_wakeup
    # 会把用户刚按下的暂停又恢复掉。
    _paused_for_wakeup = False
    try:
        ok = bool(await speaker.set_playing(False))
    except Exception as exc:  # noqa: BLE001 - 设备边界异常收敛
        logger.warning(f"[SongRequest] pause failed: {exc}")
        ok = False
    if not ok:
        return "暂停没有成功（音箱没响应）"
    logger.info("[SongRequest] paused by user")
    return "已暂停"


async def _do_resume() -> str:
    """继续播放：唤醒暂停/用户暂停后的续播（「继续播放」不能被当成切歌）。"""
    global _paused_for_wakeup
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    cancel_pending_resume()
    try:
        status = await speaker.get_playing(sync=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SongRequest] resume readback failed: {exc}")
        status = None
    if status == "playing":
        _paused_for_wakeup = False
        _arm_advance()
        return "已经在播放了"
    if status != "paused":
        return "当前没有可续播的内容"
    try:
        ok = bool(await speaker.set_playing(True))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[SongRequest] resume failed: {exc}")
        ok = False
    if not ok:
        return "继续播放没有成功（音箱没响应）"
    _paused_for_wakeup = False
    _arm_advance()  # 专辑队列：续播后重新挂「播完自动下一首」
    logger.info("[SongRequest] resumed by user")
    from core.tools.registry import suppress_reply

    suppress_reply()  # 音乐已续上：本轮收尾播报会打断它
    return "继续播放"


async def _search_same_song(session, item: dict) -> dict | None:
    """按「歌名 + 主歌手」在其它源找同一首（专辑曲目本源取不到直链时补播）。

    专辑端点仅 netease 接入，而 netease 对腾讯系版权（周杰伦等）常返回 502 ⇒
    按官方曲序连播必须能换源补同一首。只认「归一化歌名相同 + 主歌手相同」，
    避免把 Live/翻唱版本当成本曲。
    """
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    key = _norm_title(name)
    singer = _primary_singer(item.get("singer"))
    own = str(item.get("source") or "").strip().lower()
    query = f"{name} {str(item.get('singer') or '').strip()}".strip()
    for source in _sources(None):
        if source == own:
            continue
        for candidate in await _search(session, source, query):
            if _norm_title(candidate.get("name")) != key:
                continue
            cand_singer = _primary_singer(candidate.get("singer"))
            if singer and cand_singer and singer != cand_singer:
                continue
            return candidate
    return None


async def _start_item(
    speaker,
    session,
    item: dict,
    message: str | None,
    *,
    search_fallback: bool = False,
) -> tuple[bool, bool, dict, int]:
    """对单个 item 取直链并起播；返回 (成功, 回读确认, 实际播放的 item, 起播序号)。

    起播序号来自事件面 `begin_playback()`，在**下发播放命令之前**取得：之后的
    完成监听用它把上一段播放的 playing/idle 排除在本曲之外（B-03 代次绑定）。

    `message=None` 表示静默起播（专辑自动续播不打断音乐）；
    `search_fallback=True`（专辑连播）：本源取不到直链时跨源补同一首，
    实际播放的 item 随返回值带出（队列/历史按它记账）。
    """
    source = str(item.get("source") or "kuwo").strip().lower()
    url = await _pick_url(session, source, item)
    played = item
    if not url and search_fallback:
        alt = await _search_same_song(session, item)
        if alt is not None:
            alt_source = str(alt.get("source") or "").strip().lower()
            alt_url = await _pick_url(session, alt_source, alt)
            if alt_url:
                played, url = alt, alt_url
                logger.info(
                    f"[SongRequest] album track {item.get('name')!r} -> {alt_source} fallback"
                )
    if not url:
        return False, False, item, 0
    if message is None:
        # 静默起播：下界就在下发之前
        from core.device import get_playback_events

        play_seq = get_playback_events().begin_playback()
        ok = bool(await speaker.play(url=url, blocking=False))
    else:
        # 选曲播报先响：下界由 _speak_and_start 在 URL 起播前取（排除播报事件）
        ok, play_seq = await _speak_and_start(speaker, message, url)
    if not ok:
        logger.error(f"[SongRequest] play rejected by device ({source})")
        return False, False, item, play_seq
    return True, await _confirm_playing(speaker), played, play_seq


# ---- 专辑自动续播（自然播完 → 下一首，静默起播不打断） ----
# 真机 2026-10-28：只组队列不会自己往下走——第 1 首播完即静默停住。
# 复用事件面 wait_for_finish（paused 不算结束，唤醒暂停不会误判成播完）。

# 判定「自然播完」的最短播放时长比例：唤醒提示音/桥内 TTS 会顶掉当前曲，
# 事件面同样表现为 idle；真机 2026-10-28 就是这样被误判成播完（晴天只放了 63s）。
_ADVANCE_MIN_RATIO = 0.7
_ADVANCE_MIN_SLACK_S = 20.0


def _duration_s(item: dict) -> float:
    duration = item.get("duration_s")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    return 0.0


async def _wait_track_end(item: dict, since_seq: int | None = None) -> str:
    """等一首自然播完。

    返回 ``finished`` / ``weak:<reason>`` / ``unreadable``。

    只有**主源亲自观察到起播和结束**（`Completion.is_strong`）才算 finished；
    影子兜底、宽限兜底、超时、未见起播、等待期间出现过未恢复的暂停，以及跨代次
    （since_seq 之前）的证据一律弱完成——B-03 规定这些都不能驱动自动续播。
    超时不再用设备回读「补」成完成：读回 idle 只能证明现在空闲，不能证明这首播完了。
    """
    from core.device import get_playback_events

    duration = item.get("duration_s")
    seconds = (
        float(duration)
        if isinstance(duration, (int, float)) and duration > 0
        else 240.0
    )
    budget_ms = max(60_000.0, seconds * 1000.0 + 30_000.0)
    done = await get_playback_events().wait_for_finish(
        budget_ms, since_seq=since_seq
    )
    if not done.finished:
        return "unreadable"
    if done.paused:
        return "weak:paused"
    if not done.is_strong:
        return f"weak:{done.reason}"
    return "finished"


def _cancel_advance() -> None:
    """取消待续播监听（新歌/停播/切歌时调用）。

    从监听自身内部重挂（自动切歌 → `_do_switch` → `_arm_advance`）时不能取消
    自己，否则会把正在执行的切歌任务打断成 CancelledError。
    """
    global _advance_task
    task, _advance_task = _advance_task, None
    if task is None or task.done():
        return
    try:
        if task is asyncio.current_task():
            return
    except RuntimeError:
        pass
    task.cancel()


def _arm_advance() -> None:
    """专辑队列挂上续播监听（含最后一首：由 `_auto_advance` 记录「专辑播完」）。"""
    global _advance_task
    _cancel_advance()
    if not _state.get("album") or _state.get("current") is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _advance_task = loop.create_task(_auto_advance(_generation))


async def _auto_advance(generation: int) -> None:
    """等到当前曲播完就静默切下一首；切歌成功后由 `_do_switch` 再挂下一条。

    只认「真播完」：播了不足时长比例就转空闲 ⇒ 是被唤醒提示音/桥内 TTS 顶掉，
    不切歌（否则会在用户说话时抢跑，并与手动「下一首」撞车）。
    """
    while True:
        if generation != _generation or not _state.get("album"):
            return
        item = _state.get("current")
        if item is None:
            return
        started_at = float(_state.get("started_at") or 0.0)
        outcome = await _wait_track_end(item, _state.get("play_seq"))
        if generation != _generation or not _state.get("album"):
            return
        if _state.get("current") is not item:
            return  # 期间已换曲（手动切歌/新点歌）：本条监听作废
        if outcome != "finished":
            logger.info(f"[SongRequest] auto-advance stopped: {outcome}")
            return
        expected = _duration_s(item)
        played_s = time.monotonic() - started_at if started_at else 0.0
        if (
            expected
            and played_s
            and played_s < expected * _ADVANCE_MIN_RATIO
            and played_s < expected - _ADVANCE_MIN_SLACK_S
        ):
            logger.info(
                "[SongRequest] auto-advance skipped: track interrupted "
                f"({played_s:.0f}s/{expected:.0f}s)"
            )
            return
        if _state["index"] + 1 >= len(_state["candidates"]):
            logger.info("[SongRequest] album finished; auto-advance idle")
            return
        result = await _do_switch(1, announce=False)
        logger.info(f"[SongRequest] auto-advance -> {result}")
        return


async def _device_switch(delta: int) -> str:
    """桥自持队列为空时切设备自身队列（原生小爱/媒体播放器在放的情形）。

    DLNA 投送中队列归投送方，设备侧 next 通常无效——失败按「没有可切的队列」上报。
    """
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    label = "下一首" if delta == 1 else "上一首"
    try:
        ok = await (speaker.next_track() if delta == 1 else speaker.prev_track())
    except Exception as exc:  # noqa: BLE001 - 设备边界异常不上抛给对话层
        logger.warning(f"[SongRequest] device {label} failed: {exc}")
        ok = False
    if not ok:
        return "当前没有可切的队列（设备也没有在播的列表），先让我放一首吧"
    from core.tools.registry import suppress_reply

    suppress_reply()  # 设备已切歌：本轮收尾播报会打断新曲
    logger.info(f"[SongRequest] switched device queue ({label})")
    return f"已切到{label}（设备自身播放列表）"


async def _do_switch(delta: int, *, announce: bool = True) -> str:
    """切歌入口（串行化）：自动续播与手动切歌共用，杜绝并发重复起播。"""
    async with _switch_lock:
        return await _do_switch_serial(delta, announce=announce)


async def _do_switch_serial(delta: int, *, announce: bool = True) -> str:
    """next（delta=1）：候选集内后移（环形）；prev（delta=-1）：历史栈优先。

    状态迁移只在真正起播成功后落地——取不到直链/设备拒收不动队列。
    `announce=False` = 专辑自动续播：静默起播，不插播报打断音乐。
    """
    global _generation, _paused_for_wakeup
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    state = _state
    direction = "next" if delta == 1 else "prev"

    if direction == "next":
        candidates = state["candidates"]
        if not candidates:
            return await _device_switch(1)
        item = candidates[(state["index"] + 1) % len(candidates)]
        from_history = False
    else:
        if state["history"]:
            item = state["history"].pop()
            from_history = True
        elif state["candidates"] and state["index"] > 0:
            item = state["candidates"][state["index"] - 1]
            from_history = False
        elif not state["candidates"]:
            return await _device_switch(-1)
        else:
            return "没有更早的曲目了"

    async with aiohttp.ClientSession() as session:
        ok, confirmed, played, play_seq = await _start_item(
            speaker,
            session,
            item,
            _switch_text(item, direction) if announce else None,
            search_fallback=bool(state.get("album")),
        )
    if not ok:
        if from_history:
            state["history"].append(item)  # 取址失败：回滚历史栈
        return "这首暂时取不到播放地址，换一首试试"

    previous = state["current"]
    if direction == "next" and previous is not None:
        state["history"].append(previous)
        del state["history"][:-16]
    if item in state["candidates"]:
        state["index"] = state["candidates"].index(item)
    state["current"] = played
    state["started_at"] = time.monotonic()
    state["play_seq"] = int(play_seq or 0)
    # 切歌即换曲：作废旧续播任务/暂停态，并按专辑队列重新挂续播监听
    _generation += 1
    _paused_for_wakeup = False
    cancel_pending_resume()
    _arm_advance()
    logger.info(
        f"[SongRequest] switched {direction} to {played.get('name')!r} "
        f"(confirmed={confirmed}, announce={announce})"
    )
    # 同 play：切歌播报已由工具完成，跳过本轮收尾播报以免打断新曲
    from core.tools.registry import suppress_reply

    suppress_reply()
    label = "下一首" if direction == "next" else "上一首"
    suffix = "，已确认出声" if confirmed else "，未确认出声（音源可能不可达）"
    return f"已切到{label}《{played.get('name')}》-{played.get('singer')}{suffix}"


# ---- 队列状态（模块级内存；bridge 无原生队列） ----
_state: dict = {
    "current": None,  # 正在播的 item（search item dict）
    "history": [],    # prev 回退栈
    "candidates": [],  # 最近一次搜索的候选集（next 轮转用）
    "index": -1,      # 当前曲在候选集中的下标
    "album": False,   # 队列是否来自专辑连播（next/prev 允许跨源补同一首）
    "started_at": 0.0,  # 当前曲起播时刻（monotonic；自动续播判断「真播完」用）
    "play_seq": 0,    # 起播前的事件序号：完成监听只认它之后的事件（B-03）
}
