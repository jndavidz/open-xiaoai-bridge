"""CCH 设备专属播放历史（票：播放历史 UI 接管，2026-11-23）。

架构（用户裁决）：播放历史 = 设备侧状态，每设备独立；**不用 Songloft 全局历史**
（那是按 context 聚合的服务端视角，不是"这台 CCH 实际播过什么"）。

真相源 = 本模块维护的 CCH 历史 deque（bridge 点歌成功时 note），全量写设备文件
`/data/local/tmp/cch_agmc_host/play_history.json`（经 9102 historyfile op），
gadget onFinished hook 检测 getAllUserPlayerHistoryV3 → 读该文件替换 data.model[]。

与 favfile（收藏）同构：bridge 持有真相、全量下发、设备文件供 UI 读。
"""
from __future__ import annotations

import asyncio
import json
from collections import deque

from core.utils.logger import logger

# 最近播放上限（原厂历史 tab 一页 30；保留 30）
_MAX = 30
_items: deque = deque(maxlen=_MAX)


def _norm_id(item: dict) -> str:
    return str(item.get("id") or item.get("rawId") or item.get("name") or "").strip().lower()


def note(item: dict) -> None:
    """点歌成功时登记一首（按 id 去重、最新在前）。fire-and-forget 写设备文件。"""
    rid = _norm_id(item)
    if not rid:
        return
    # 去重：同 id 移到最前
    for i, it in enumerate(_items):
        if _norm_id(it) == rid:
            del _items[i]
            break
    _items.appendleft(dict(item))
    asyncio.ensure_future(_push())


def history_item_schema(items: list, fav_rids: set | None = None) -> list:
    """bridge item 列表 → 原厂播放历史 item schema（纯函数可单测）。
    与收藏 schema 同构，差异：playFrom=playHistory + firstType/secondType；
    id 用 rawId（字符串 hash，非数字也保留——原厂 UI 主要按 rawId 点播）。

    票 01h：collect 字段（历史 tab 红心）必须由收藏真相源校准——
    传 fav_rids（收藏表 rawID 集合，大小写不敏感）时，按唯一身份覆盖 item 自带的
    collect 死快照；不传则保留旧行为（item 自带 collect），向后兼容。
    """
    norm_fav = {str(x).strip().lower() for x in (fav_rids or set())}
    out = []
    for pos, it in enumerate(items or []):
        name = str(it.get("name") or it.get("title") or "").strip()
        rid = str(it.get("id") or it.get("rawId") or "").strip()
        if not name or not rid:
            continue
        src = str(it.get("source") or "kwqq").upper()
        artist = str(it.get("artist") or it.get("singer") or "").strip()
        album = str(it.get("album") or "").strip()
        cover = str(it.get("cover") or "").strip()
        try:
            duration = int(float(it.get("duration_s") or it.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        # 收藏态：有真相源集合按 rawID 校准（红心反映真实收藏），否则沿用 item 自带
        collect = (rid.lower() in norm_fav) if fav_rids is not None else bool(it.get("collect"))
        from core.music.cch_schema import native_item_schema
        out.append(native_item_schema(
            sid=0, raw_id=rid, source=str(it.get("source") or "kwqq"),
            title=name, artist=artist, album=album, cover=cover,
            duration=duration, pos=pos, collect=collect,
            play_from="playHistory", history_extras=True, is_audition=False))
    return out


def _extract_identity(it: dict) -> dict | None:
    """从历史 item 提取唯一身份 (rawID, source) + 展示字段。"""
    rid = str(it.get("rawId") or it.get("id") or "").strip()
    src = str(it.get("source") or "").strip().lower()
    name = str(it.get("title") or it.get("name") or "").strip()
    if not rid or not src:
        return None
    authors = it.get("authors") or []
    artist = ""
    if isinstance(authors, list) and authors and isinstance(authors[0], dict):
        artist = str(authors[0].get("title") or "").strip()
    return {"rawId": rid, "source": src, "name": name, "artist": artist}


def get_by_pos_from_items(items: list, pos) -> dict | None:
    """纯函数：按显示索引 pos 从历史 items 反查身份（可单测）。"""
    try:
        idx = int(pos)
    except (TypeError, ValueError):
        return None
    if idx < 0 or idx >= len(items) or not isinstance(items[idx], dict):
        return None
    return _extract_identity(items[idx])


def get_by_rawid_from_items(items: list, raw_id) -> dict | None:
    """纯函数：按 rawID 从历史 items 反查完整 item（大小写不敏感，可单测）。

    票 01：FAVADD 新增收藏时，目标曲不在收藏表、需从历史真相源拿元数据走
    ensure_song 重建；返回原厂 schema item（含 cover/authors/duration），
    由调用方还原成 ensure_song 格式。无匹配/无身份 → None（fail-open）。
    """
    want = str(raw_id or "").strip().lower()
    if not want:
        return None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        rid = str(it.get("rawId") or it.get("id") or "").strip().lower()
        if rid and rid == want:
            return it
    return None


def item_for_favorite_from_history(hit: dict) -> dict:
    """原厂历史 schema item → ensure_song 吃的 item（纯函数，可单测）。

    还原字段：rawId→id、title→name、source→source、authors[0].title→artist、
    cover.img→cover、duration→duration_s。albumItem 仅收藏 tab 有，历史 tab
    缺省无，album 可空（ensure_song 不强求）。
    """
    out = {
        "id": str(hit.get("rawId") or hit.get("id") or "").strip(),
        "name": str(hit.get("title") or hit.get("name") or "").strip(),
        "source": str(hit.get("source") or "").strip().lower(),
    }
    authors = hit.get("authors") or []
    if isinstance(authors, list) and authors and isinstance(authors[0], dict):
        out["artist"] = str(authors[0].get("title") or "").strip()
    cover = hit.get("cover")
    if isinstance(cover, dict):
        out["cover"] = str(cover.get("img") or "").strip()
    try:
        out["duration_s"] = float(hit.get("duration") or 0)
    except (TypeError, ValueError):
        out["duration_s"] = 0
    album_item = hit.get("albumItem")
    if isinstance(album_item, dict):
        out["album"] = str(album_item.get("title") or "").strip()
    # §2.77-58：咪咕 by-id 取链需 copyrightId。设备 play_history.json 的原厂 schema
    # 不带该字段，但 bridge 内存 deque 的门面 item（点歌时 note）带 extra.copyrightId——
    # 有则透传，ensure_song 写进 source_data.c；缺则留空（其它平台不需要，fail-open）。
    extra = hit.get("extra")
    if isinstance(extra, dict) and extra.get("copyrightId"):
        out["extra"] = {"copyrightId": str(extra["copyrightId"]).strip()}
    return out


async def _get_from_truth(key, from_items) -> dict | None:
    """历史真相源统一查询骨架：先设备 play_history.json 回读，内存 deque fallback。

    重启后内存丢但设备文件在，点击反查不受影响；设备回读失败降级内存（fail-open）。
    key/from_items 由调用方给定（pos→身份子集 / rawID→完整 item），骨架不感知语义。"""
    from core.music import cch_cast

    try:
        ok, payload = await cch_cast.musicctl("historyfile", {})
        if ok and isinstance(payload, dict) and isinstance(payload.get("items"), list):
            hit = from_items(payload["items"], key)
            if hit:
                return hit
    except Exception:  # noqa: BLE001 - 设备回读失败降级内存
        pass
    return from_items(list(_items), key)


async def get_by_rawid(raw_id) -> dict | None:
    """按 rawID 从历史真相源取完整 item（票 01：FAVADD 重建收藏用）。"""
    return await _get_from_truth(raw_id, get_by_rawid_from_items)


async def get_by_pos(pos) -> dict | None:
    """按显示索引 pos 取历史条目唯一身份。"""
    return await _get_from_truth(pos, get_by_pos_from_items)


# 上次成功读到的收藏 rawID 集合（票 10h）：Songloft 临时读失败时保留上次值，
# 避免降级空集把已收藏曲红心全抹成空心（假阴性）。None = 从未成功读过。
_last_fav_rids: set[str] | None = None


async def load_favorite_rawids() -> set[str] | None:
    """从收藏真相源（Songloft 收藏歌单）取 rawID 集合，供历史红心校准。

    返回 None 表示"未知"（读失败且无历史缓存）→ _push 不校准 collect，保留 item 自带值；
    读失败但有上次成功缓存 → 返回上次集合（宁可短暂陈旧，不做假阴性全灭）。
    """
    from core.music import songloft_api

    global _last_fav_rids
    try:
        import aiohttp

        async with aiohttp.ClientSession() as http:
            pl = await songloft_api.favorites_playlist_id(http=http)
            songs = await songloft_api.playlist_songs(pl, http=http)
    except Exception as exc:  # noqa: BLE001 - 收藏读失败：用上次缓存，不假阴性全灭
        logger.debug(f"[CchHistory] load_favorite_rawids failed, use last cache: "
                     f"{type(exc).__name__}", module="CchHistory")
        return _last_fav_rids

    out = set()
    for s in songs or []:
        rid = songloft_api.song_rawid(s)
        if rid:
            out.add(rid.lower())
    _last_fav_rids = out
    return out


async def _push() -> None:
    """完整历史 → 原厂 schema（按收藏真相源校准红心）→ 9102 historyfile 全量写设备文件。"""
    from core.music import cch_cast

    fav_rids = await load_favorite_rawids()
    items = history_item_schema(list(_items), fav_rids=fav_rids)
    ok, payload = await cch_cast.musicctl("historyfile", {"items": items})
    logger.info(f"[CchHistory] push n={len(items)} fav={len(fav_rids)} ok={ok} "
                f"status={str(payload)[:120]}", module="CchHistory")
