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
            "按歌名/歌手在线搜歌并马上在音箱播放（320k 音质）。"
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
            url, params=params, timeout=aiohttp.ClientTimeout(total=8)
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
            items = await _search(session, source, query)
            if not items:
                continue
            for item in items:
                ok, confirmed = await _start_item(
                    speaker, session, item, _announce_text(item)
                )
                if not ok:
                    continue  # 无直链/加密件/设备拒收：候选内继续，不行再落下一源
                _note_played(item, items)
                logger.info(
                    f"[SongRequest] playing {item.get('name')!r} via {source} "
                    f"(confirmed={confirmed})"
                )
                suffix = (
                    "，已确认出声" if confirmed
                    else "，未确认出声（音源可能不可达，可让我停止）"
                )
                return f"已开始播放《{item.get('name')}》-{item.get('singer')}（音源 {source}）{suffix}"

    logger.info(f"[SongRequest] no playable result for {query!r} in {sources}")
    return f"没有找到能播放的《{query}》，换个说法或换个源试试"


def _note_played(item: dict, candidates: list[dict]) -> None:
    """落队列状态：当前曲 + 历史 + 候选集与下标（内存态，不做持久化）。"""
    state = _state
    if state["current"] is not None:
        state["history"].append(state["current"])
        del state["history"][:-16]  # 防涨：prev 只回看最近若干首
    state["current"] = item
    state["candidates"] = list(candidates)
    try:
        state["index"] = state["candidates"].index(item)
    except ValueError:
        state["candidates"] = [item]
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


async def _do_stop() -> str:
    """停止播放：设备侧全链路停 + 清队列（设备可能还在放其它来源，也要能停）。"""
    speaker = get_speaker()
    if speaker is None:
        return "音箱服务还没就绪"
    await speaker.stop_device_audio()
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
