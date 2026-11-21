"""Songloft 统一入口（bridge 侧唯一的音乐服务端）。

分工（票 24）：
- **Songloft** = 音乐服务端：聚合搜索 / 跨源歌词（kwqq 插件）/ 托管票据链 / 歌单与历史。
- **kwqq-api** = 统一门面（musicdl）：逐源解析，只由 Songloft 插件直接消费。
- **bridge** 只认 Songloft；Songloft 不可用时才回落到门面逐源（保留兜底，票 24 用户裁决）。

鉴权/配置复用 `core.music.song_bridge` 的单点实现（token 缓存、songloft_* 配置），
本模块不重复登录、不另建缓存。

插件 API（经 Songloft 代理 `/api/v1/jsplugin/kwqq/*`，需 JWT）：
- `POST api/search {keyword,page,page_size}` → `{results:[{title,artist,album,duration,cover_url,source_data:{s,i,c?}}]}`
- `GET  lyric-search?title=&artist=` → `{lyric, tlyric?, lxlyric?}`（跨源回搜；lxlyric = 逐字）
"""
from __future__ import annotations

import re

import aiohttp

_PLUGIN_PATH = "/api/v1/jsplugin/kwqq"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


class SongloftError(RuntimeError):
    """Songloft 不可用（未配置/鉴权失败/HTTP 非 2xx）——调用方据此回落门面。"""


def _song_bridge():
    from core.music import song_bridge

    return song_bridge


def base_url() -> str:
    base = str(_song_bridge()._songloft_cfg("songloft_base", "") or "").rstrip("/")
    if not base:
        raise SongloftError("songloft_base not configured")
    return base


async def _request(http, method: str, path: str, *, params=None, json_body=None) -> dict:
    base = base_url()
    token = await _song_bridge()._songloft_token(base, http)
    headers = {"Authorization": f"Bearer {token}"}
    async with http.request(method, f"{base}{path}", params=params, json=json_body,
                            headers=headers, timeout=_HTTP_TIMEOUT) as resp:
        if resp.status != 200:
            raise SongloftError(f"{method} {path}: http {resp.status}")
        data = await resp.json(content_type=None)
    if not isinstance(data, dict):
        raise SongloftError(f"{method} {path}: unexpected body")
    return data


def to_item(result: dict) -> dict:
    """Songloft 搜索结果 → 门面 item 形状（bridge 既有消费方只认这一种）。"""
    sd = (result or {}).get("source_data") or {}
    extra = {"copyrightId": str(sd["c"])} if sd.get("c") else {}
    artist = str(result.get("artist") or "")
    return {
        "name": str(result.get("title") or ""),
        "artist": artist,
        "singer": artist,
        "album": str(result.get("album") or ""),
        "cover": str(result.get("cover_url") or ""),
        "duration_s": float(result.get("duration") or 0),
        "source": str(sd.get("s") or ""),
        "id": str(sd.get("i") or ""),
        "extra": extra,
    }


_LX_WORD_TAG = re.compile(r"<[^<>]*>")


def lrc_from_lxlyric(text: str) -> str:
    """逐字歌词（洛雪格式）→ 行级 LRC：去掉 `<偏移,时长>` 标记，保留行时间戳。

    用于 lxlyric 有而 lyric 空时的降级（CCH 原厂 LrcView 是否吃逐字尚未验证）。
    """
    out = []
    for line in str(text or "").splitlines():
        stripped = _LX_WORD_TAG.sub("", line).strip()
        if stripped:
            out.append(stripped)
    return "\n".join(out)


def pick_lrc(payload: dict) -> str:
    """歌词取值：`lyric` 优先；空则用 `lxlyric` 去标记降级；都空返回空串。"""
    lyric = str((payload or {}).get("lyric") or "").strip()
    if lyric:
        return lyric
    return lrc_from_lxlyric((payload or {}).get("lxlyric") or "")


async def search(keyword: str, page: int = 1, page_size: int = 15,
                 *, http: aiohttp.ClientSession) -> list[dict]:
    """聚合搜索（插件按 SEARCH_SOURCE_ORDER 交错合并）。失败抛 SongloftError。"""
    body = {"keyword": str(keyword or "").strip(), "page": int(page),
            "page_size": int(page_size)}
    if not body["keyword"]:
        return []
    data = await _request(http, "POST", f"{_PLUGIN_PATH}/api/search", json_body=body)
    results = data.get("results")
    if not isinstance(results, list):
        return []
    return [_i for _i in (to_item(r) for r in results if isinstance(r, dict))
            if _i["source"] and _i["id"]]


async def lyric(title: str, artist: str = "",
                *, http: aiohttp.ClientSession) -> dict:
    """跨源歌词回搜：`{lyric, tlyric?, lxlyric?}`；lxlyric = 逐字（洛雪格式）。

    空歌词是合法结果（插件找不到时返回 `{lyric: ""}`）；失败抛 SongloftError。
    """
    if not str(title or "").strip():
        return {}
    return await _request(http, "GET", f"{_PLUGIN_PATH}/lyric-search",
                          params={"title": str(title).strip(),
                                  "artist": str(artist or "").strip()})
