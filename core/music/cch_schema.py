"""CCH 原厂列表 item schema 单一实现（2026-11-25 架构统一）。

收藏 tab（`songloft_api.favorite_item_schema`）与播放历史 tab
（`cch_history.history_item_schema`）此前各写一份 90% 相同的原厂 item schema，
字段漂移风险高。本模块提供公共基础 + 两个差异覆盖，输出与重构前逐字段一致。

字段依据 facts §2.76-4 真实 V2 响应；凭据纪律：只取元数据，screenPlayUrl 用固定
genie:// 深链 + (rawId, source)，绝不下发票据 URL。
"""
from __future__ import annotations

import json

from core.music.songloft_api import _fav_authors, _fav_cover  # 复用，零字段漂移


def _screen_play_url(raw_id: str, source: str) -> str:
    return ("genie://com.alibaba.ailabs.genie.musicplayer?cmd=start"
            "&data={\"type\":\"content\"}&contentList="
            "[{\"rawId\":\"%s\",\"source\":\"%s\"}]" % (raw_id, source))


def native_item_schema(*, sid: int, raw_id: str, source: str, title: str,
                       artist: str, album: str, cover: str, duration: int,
                       pos: int, collect: bool, play_from: str,
                       display_title: str | None = None,
                       include_album_item: bool = False,
                       history_extras: bool = False,
                       is_audition: bool | None = None) -> dict:
    """构造原厂列表 item（收藏/历史共用）。

    Args:
        sid: 数字 song id（收藏用真 id；历史为 0）
        raw_id/source: 唯一身份
        title: 纯曲名；display_title: 收藏栏显示的「歌名-《专辑》-平台」（缺省用 title）
        include_album_item: 收藏 tab 带 albumItem；历史 tab 不带
        history_extras: 历史 tab 额外 firstType/secondType/timeStamp/currentPlay/offline/url
    """
    src = str(source or "kwqq").upper()
    item = {
        "duration": int(duration or 0), "albumRawId": "0", "text": None,
        "otherTypeIds": None, "lyric": None, "lastPage": None,
        "id": int(sid or 0), "source": src, "category": "audio", "type": "music",
        "isCharge": False, "isAudition": is_audition, "copyright": 0,
        "title": display_title or title, "rawId": raw_id, "pos": pos,
        "packageType": None, "audioType": None, "singer": None,
        "collect": bool(collect), "playFrom": play_from,
        "vip": False, "chargeStatus": None,
        "screenPlayUrl": _screen_play_url(raw_id, src),
        "sourceIcon": None, "playUrls": [], "bindingSourceList": None,
        "extendInfo": None, "itemType": {"name": "CONTENT"}, "valid": {"name": "VALID"},
        "cover": _fav_cover(cover), "authors": _fav_authors(artist, src),
    }
    if include_album_item:
        item["albumItem"] = ({
            "id": 0, "rawId": "0", "source": src, "title": album,
            "category": "audio", "type": "music", "isCharge": False,
            "isAudition": False, "copyright": 0, "boughtStatus": False,
            "albumType": {"name": "ALBUM"}, "finished": {"name": "NOT_FINISHED"},
            "itemType": {"name": "ALBUM"}, "valid": {"name": "VALID"},
            "online": {"name": "OFFLINE"}, "cover": _fav_cover(cover),
            "alias": [album] if album else [], "authors": None,
            "appIds": ["160"], "contentCopyright": ["has_genie_right"],
        } if album else None)
        item["revealLabel"] = None
    if history_extras:
        item.update({"firstType": "content-audio", "secondType": "music",
                     "timeStamp": None, "currentPlay": None, "offline": False,
                     "url": None, "revealLabel": None})
    return item
