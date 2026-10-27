"""工具：song_request（语音点歌，kw-qq-music-api spike，票 01）。

数据面：NAS kw-qq-music-api（默认 http://10.10.10.2:3003，内网 10.10.10.0/24
免 key）。两跳：`/{source}/search` 搜歌 → `/{source}/song/url` 取 320k 直链，
经 `speaker.play(url=...)`（announce 双闸门内建）在 LX06 播放。

契约事实源：musicdl 仓库 docs/API-REFERENCE.md v1.1——业务端点统一信封
`{code,msg,data,timestamp}`，`code != 200` 即业务失败（HTTP 200 不代表成功）；
`ekey` 加密件不进播放层（ADR-0002）；quality 固定 320k（消费铁律 ≤320k mp3）。

分级：RISK = "a"（复述即执行——点歌副作用小、可即时停止，spec 裁决 1）；
选曲确认取最轻形态：播报第一首并复述曲名/歌手，「换一首」由队列 next 支持，
不扩 guardrails 选项式多轮（现框架是/否单槽）。

队列：模块级内存状态（当前曲 + 历史 + 候选集），next/prev 就地切 URL 播放，
stop 走 speaker.stop_device_audio()（bridge 无原生队列，spec 裁决 3）。
"""

from __future__ import annotations

import asyncio
import re

import aiohttp

from core.ref import get_speaker
from core.utils.config import ConfigManager
from core.utils.logger import logger

NAME = "song_request"

# spec 裁决 1：a = 复述即执行（guardrails A 级一次确认）
RISK = "a"

# 消费铁律（spec §0）：播放类消费方只吃 ≤320k mp3
_QUALITY = "320k"

# 单次搜索拉取的候选数（「换一首」在候选集内轮转）
_SEARCH_LIMIT = 5

# 默认源策略（要点 6）：按序尝试、首源失败回落。
# 排除 bilibili——其直链必须随 Referer/UA headers，设备 mediaplayer
# player_play_url 无 headers 通道 ⇒ 403；migu 不入默认列表
# （copyrightId 回传链复杂化 spike，留配置可加）。
_DEFAULT_SOURCES = ("kuwo", "netease", "qq")

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
            "action=play 播放 query 指定的歌；action=next/prev 切上一首下一首；"
            "action=stop 停止。仅 action=play 需要 query。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "next", "prev", "stop"],
                    "description": "play=点歌播放，next/prev=切换，stop=停止",
                },
                "query": {
                    "type": "string",
                    "description": "搜索词（歌名，或歌名+歌手）。action=play 时必填",
                },
                "source": {
                    "type": "string",
                    "description": "可选：指定音源（kuwo/netease/qq），默认按配置顺序",
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
    """参数校验：action 白名单 + play 必带 query（返回拒绝原因即拒绝）。"""
    action = str((args or {}).get("action") or "").strip().lower()
    if action not in ("play", "next", "prev", "stop"):
        return "action 必须是 play/next/prev/stop"
    if action == "play" and not str((args or {}).get("query") or "").strip():
        return "play 需要 query（歌名或歌手）"
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
    if action == "stop":
        return await _do_stop()
    if action == "next":
        return await _do_switch(1)
    if action == "prev":
        return await _do_switch(-1)
    return await _do_play(str(args.get("query") or "").strip(), args.get("source"))


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
    return url


# ---- 源策略（要点 6） ----


def _sources(requested) -> list[str]:
    """按序源列表：显式指定时限定在配置集合内；bilibili 任何情况下排除。"""
    configured = _cfg("sources") or list(_DEFAULT_SOURCES)
    configured = [str(s).strip().lower() for s in configured if s]
    if requested:
        src = str(requested).strip().lower()
        if src == "bilibili":
            return []
        return [src] if src in configured else []
    return [s for s in configured if s != "bilibili"]


# ---- 播放与回读（要点 4） ----


def _announce_text(item: dict) -> str:
    """选曲播报文案（复述曲名/歌手，最轻确认形态）。"""
    name = str(item.get("name") or "").strip() or "未知名曲目"
    singer = str(item.get("singer") or "").strip()
    text = f"为你找到《{name}》"
    if singer:
        text += f"，{singer}"
    return text + "，这就为你播放"


async def _speak_and_start(speaker, message: str, url: str) -> bool:
    """经 announce 双闸门播报选曲文案后起播 URL（非阻塞）。

    文案撞 KWS 词表（歌名/歌手名不可控）时降级为通用文案重试；
    仍被拒则不打播报、仅起播（announce 拒绝的是文案，不是播放本身）。
    """
    from core.services.speaker import AnnounceRejected

    for text in (message, _FALLBACK_ANNOUNCE):
        try:
            async with speaker.announce(text=text, reason="song_request", wait=False):
                await speaker.play(text=text)
                return bool(await speaker.play(url=url, blocking=False))
        except AnnounceRejected as exc:
            logger.warning(f"[SongRequest] announce rejected (word={exc.word}); fallback")
            continue
    return bool(await speaker.play(url=url, blocking=False))


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
        for source in sources:
            raw_items = _drop_query_variants(await _search(session, source, query), query)
            items = _dedupe_items(raw_items)
            if not items:
                continue
            # 尝试用原始候选（同曲其他版本可作为取链失败的兜底），队列用去重结果
            for item in raw_items:
                ok, confirmed = await _start_item(
                    speaker, session, item, _announce_text(item)
                )
                if not ok:
                    continue  # 无直链/加密件/设备拒收：候选内继续，不行再落下一源
                queue = _queue_for(item, items)
                if len(queue) < 2:
                    # 队列只剩同一首：按歌手补别的歌后再按当前曲过滤
                    extra = await _extend_with_singer(session, source, items)
                    queue = _queue_for(item, list(items) + extra)
                _note_played(item, queue)
                logger.info(
                    f"[SongRequest] playing {item.get('name')!r} via {source} "
                    f"(confirmed={confirmed})"
                )
                # 工具已自行播报并起播：本轮收尾 TTS 会打断音乐，置免播
                from core.tools.registry import suppress_reply

                suppress_reply()
                suffix = (
                    "，已确认出声" if confirmed
                    else "，未确认出声（音源可能不可达，可让我停止）"
                )
                return f"已开始播放《{item.get('name')}》-{item.get('singer')}（音源 {source}）{suffix}"

    logger.info(f"[SongRequest] no playable result for {query!r} in {sources}")
    return f"没有找到能播放的《{query}》，换个说法或换个源试试"


def _note_played(item: dict, candidates: list[dict]) -> None:
    """落队列状态：当前曲 + 历史 + 候选集（同一首只占一个队列位）。"""
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


# ---- 唤醒中断：暂停而不是停播（有队列时），处理完自动续播 ----
# 真机 2026-10-27：原生「小爱同学」唤醒走 wakeup_session.on_interrupt，原策略
# 是 stop_device_audio（停），导致「调低音量」这类命令必须牺牲当前曲目。
# 这里改为 pause + 空闲后 auto-resume；新点歌（_note_played）会让旧续播任务作废。
_RESUME_POLL_S = 1.0
_RESUME_IDLE_STREAK = 2  # 连续空闲 2 秒才续播，避开自己的 TTS 尾巴
_RESUME_MAX_S = 30.0

_resume_task = None
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
    """续播（若仍持有同一首）。幂等：已在播则直接 True。"""
    global _paused_for_wakeup
    cancel_pending_resume()
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
    speaker = get_speaker()
    if speaker is None:
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


async def _do_stop() -> str:
    """停止播放：设备侧全链路停 + 清队列（设备可能还在放其它来源，也要能停）。"""
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    await speaker.stop_device_audio()
    cancel_pending_resume()
    global _paused_for_wakeup
    _paused_for_wakeup = False
    _state.update({"current": None, "candidates": [], "index": -1, "history": []})
    logger.info("[SongRequest] stopped and queue cleared")
    return "已停止播放，队列已清空"


async def _start_item(speaker, session, item: dict, message: str) -> tuple[bool, bool]:
    """对单个 item 取直链并起播；返回 (成功, 回读确认)。失败返回 (False, False)。"""
    source = str(item.get("source") or "kuwo").strip().lower()
    url = await _pick_url(session, source, item)
    if not url:
        return False, False
    if not await _speak_and_start(speaker, message, url):
        logger.error(f"[SongRequest] play rejected by device ({source})")
        return False, False
    return True, await _confirm_playing(speaker)


async def _do_switch(delta: int) -> str:
    """next（delta=1）：候选集内后移（环形）；prev（delta=-1）：历史栈优先。

    状态迁移只在真正起播成功后落地——取不到直链/设备拒收不动队列。
    """
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    state = _state
    direction = "next" if delta == 1 else "prev"

    if direction == "next":
        candidates = state["candidates"]
        if not candidates:
            return "当前没有点歌队列，先让我放一首吧"
        item = candidates[(state["index"] + 1) % len(candidates)]
        from_history = False
    else:
        if state["history"]:
            item = state["history"].pop()
            from_history = True
        elif state["candidates"] and state["index"] > 0:
            item = state["candidates"][state["index"] - 1]
            from_history = False
        else:
            return "没有更早的曲目了"

    async with aiohttp.ClientSession() as session:
        ok, confirmed = await _start_item(
            speaker, session, item, _switch_text(item, direction)
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
    state["current"] = item
    logger.info(
        f"[SongRequest] switched {direction} to {item.get('name')!r} "
        f"(confirmed={confirmed})"
    )
    # 同 play：切歌播报已由工具完成，跳过本轮收尾播报以免打断新曲
    from core.tools.registry import suppress_reply

    suppress_reply()
    label = "下一首" if direction == "next" else "上一首"
    suffix = "，已确认出声" if confirmed else "，未确认出声（音源可能不可达）"
    return f"已切到{label}《{item.get('name')}》-{item.get('singer')}{suffix}"


# ---- 队列状态（模块级内存；bridge 无原生队列） ----
_state: dict = {
    "current": None,  # 正在播的 item（search item dict）
    "history": [],    # prev 回退栈
    "candidates": [],  # 最近一次搜索的候选集（next 轮转用）
    "index": -1,      # 当前曲在候选集中的下标
}
