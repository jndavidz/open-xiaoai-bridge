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
    from core.music.songloft_api import _fav_authors, _fav_cover

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
        out.append({
            "duration": duration, "albumRawId": "0", "text": None,
            "otherTypeIds": None, "lyric": None, "lastPage": None,
            "id": 0, "source": src, "category": "audio", "type": "music",
            "isCharge": False, "isAudition": False, "copyright": 0,
            "title": name, "rawId": rid, "pos": pos,
            "packageType": None, "audioType": None, "singer": None,
            "collect": bool(it.get("collect")), "playFrom": "playHistory",
            "vip": False, "chargeStatus": None,
            "screenPlayUrl": ("genie://com.alibaba.ailabs.genie.musicplayer?cmd=start"
                              "&data={\"type\":\"content\"}&contentList="
                              "[{\"rawId\":\"%s\",\"source\":\"%s\"}]" % (rid, src)),
            "sourceIcon": None, "playUrls": [], "bindingSourceList": None,
            "extendInfo": None, "itemType": {"name": "CONTENT"},
            "valid": {"name": "VALID"}, "cover": _fav_cover(cover),
            "authors": _fav_authors(artist, src),
            "firstType": "content-audio", "secondType": "music",
            "timeStamp": None, "currentPlay": None, "offline": False,
            "url": None, "revealLabel": None,
        })
    return out


async def _push() -> None:
    """完整历史 → 原厂 schema → 9102 historyfile 全量写设备文件。"""
    from core.music import cch_cast

    items = history_item_schema(list(_items))
    ok, payload = await cch_cast.musicctl("historyfile", {"items": items})
    logger.info(f"[CchHistory] push n={len(items)} ok={ok} status={str(payload)[:120]}",
                module="CchHistory")
