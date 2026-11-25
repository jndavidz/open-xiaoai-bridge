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


def history_item_schema(items: list) -> list:
    """bridge item 列表 → 原厂播放历史 item schema（纯函数可单测）。
    与收藏 schema 同构，差异：playFrom=playHistory + firstType/secondType；
    id 用 rawId（字符串 hash，非数字也保留——原厂 UI 主要按 rawId 点播）。"""
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
        from core.music.cch_schema import native_item_schema
        out.append(native_item_schema(
            sid=0, raw_id=rid, source=str(it.get("source") or "kwqq"),
            title=name, artist=artist, album=album, cover=cover,
            duration=duration, pos=pos, collect=bool(it.get("collect")),
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


async def get_by_pos(pos) -> dict | None:
    """按显示索引 pos 取历史条目唯一身份。真相源 = 设备 play_history.json
    （经 9102 historyfile read 回读），bridge 内存 deque 仅作 fallback——
    重启后内存丢但设备文件在，点击反查不受影响。"""
    from core.music import cch_cast

    try:
        ok, payload = await cch_cast.musicctl("historyfile", {})
        if ok and isinstance(payload, dict) and isinstance(payload.get("items"), list):
            hit = get_by_pos_from_items(payload["items"], pos)
            if hit:
                return hit
    except Exception:  # noqa: BLE001 - 设备回读失败降级内存
        pass
    return get_by_pos_from_items(list(_items), pos)


async def _push() -> None:
    """完整历史 → 原厂 schema → 9102 historyfile 全量写设备文件。"""
    from core.music import cch_cast

    items = history_item_schema(list(_items))
    ok, payload = await cch_cast.musicctl("historyfile", {"items": items})
    logger.info(f"[CchHistory] push n={len(items)} ok={ok} status={str(payload)[:120]}",
                module="CchHistory")
