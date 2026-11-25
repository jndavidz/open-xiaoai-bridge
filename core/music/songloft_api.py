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

import json
import re

import aiohttp

_PLUGIN_PATH = "/api/v1/jsplugin/kwqq"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)
# 收藏变更长轮询端点最多挂起 25s（Songloft services/favorite_changes.go），留足余量。
_LONG_POLL_TIMEOUT = aiohttp.ClientTimeout(total=40)


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


async def _request(http, method: str, path: str, *, params=None, json_body=None,
                   timeout: aiohttp.ClientTimeout | None = None) -> dict:
    base = base_url()
    token = await _song_bridge()._songloft_token(base, http)
    headers = {"Authorization": f"Bearer {token}"}
    async with http.request(method, f"{base}{path}", params=params, json=json_body,
                            headers=headers, timeout=timeout or _HTTP_TIMEOUT) as resp:
        if not 200 <= resp.status < 300:
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


# ---- UI 队列载荷（票 21）：门面统一构造，所有消费者（CCH / songloft App / 未来其他）同源 ----
# 门面 item 的 source = source_data.s（kwqq 插件产出的平台标识），是平台字段的唯一真相；
# 不在此处另建映射或透传未经门面归一的字段。平台展示名由各消费者按门面 source 自行渲染。

def queue_topn() -> int:
    """UI 队列下发条数（票 21）；缺省 20，边界 1..100（宿主 QueueWriter 上限）。"""
    from core.utils.config import ConfigManager

    try:
        n = int(ConfigManager.instance().get_app_config("song_request.cch_queue_topn", 20))
    except (TypeError, ValueError):
        n = 20
    return max(1, min(100, n))


def queue_payload(items: list) -> str:
    """门面 item 列表 → 宿主队列 itemsJson（票 21，UI 播放列表层）。

    只投影元数据（曲名/歌手/专辑/封面/平台/id/时长），**绝不含 audioUrl/票据 URL**——
    Songloft 票据 DefaultTTL=30min，整队列预票据会过期 ⇒ audioUrl 播放时逐曲取票。
    字段别名对齐宿主 QueueWriter.newSong；平台 = 门面 item.source，原样传递不加工。
    """
    out = []
    for item in (items or [])[:queue_topn()]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        sid = str(item.get("id") or "").strip()
        if not name or not sid:
            continue  # 无 id 无法逐曲取票，跳过
        try:
            duration = int(float(item.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration = 0
        out.append({
            "name": name,
            "artist": str(item.get("artist") or item.get("singer") or ""),
            "album": str(item.get("album") or ""),
            "cover": str(item.get("cover") or ""),
            "source": str(item.get("source") or ""),
            "id": sid,
            "duration_s": duration,
        })
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


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


def search_params_for_intent(song: str, artist: str) -> tuple[str, str]:
    """点播意图 (song, artist) → 分类搜索参数 (keywords, type)（纯函数，可单测）。

    用户裁决 2026-11-25：LLM 只给语义字段，bridge 决定搜索参数。
    - 仅 artist → 用歌手名做 type=music 搜索（实测普通搜索返回该歌手曲目，
      to_item 只需要 source_data.s/i；type=artist 返回的是歌手对象 {id,name,avatar}
      无 source_data，会被 to_item 过滤空——2026-11-25 真机 VERIFIED 否决）
    - 仅 song 或两者都有 → type=music（单曲搜索，artist 作版本过滤由调用方处理）
    """
    song = str(song or "").strip()
    artist = str(artist or "").strip()
    return song or artist, "music"


async def browse_search(keywords: str, search_type: str, *, page: int = 1,
                        limit: int = 20, http: aiohttp.ClientSession) -> list[dict]:
    """Songloft 插件层分类搜索（与 App 同源，非直连门面）——票 02。

    POST /api/v1/jsplugin/kwqq/browse/search {keywords, type, page, limit}
    type ∈ music|album|artist|sheet|mv（kwqq main.js BROWSE_ROUTES VERIFIED）。
    返回 items 经 to_item 映射为 bridge item；失败抛 SongloftError（调用方回退 /api/search）。
    """
    body = {"keywords": str(keywords or "").strip(),
            "type": str(search_type or "music").strip().lower(),
            "page": max(1, int(page)), "limit": max(1, min(50, int(limit)))}
    if not body["keywords"]:
        return []
    data = await _request(http, "POST", f"{_PLUGIN_PATH}/browse/search", json_body=body)
    items = data.get("items")
    if not isinstance(items, list):
        return []
    return [_i for _i in (to_item(r) for r in items if isinstance(r, dict))
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


# ---- 收藏（票 13：只写 Songloft，屏蔽原厂 tap）----
# 落点 = Songloft 内置收藏歌单（`built_in` 标签、type=normal），成员是库内歌曲 ID；
# 点播曲目先经 /songs/remote 以插件引用（kwqq + source_data + dedup_key）入库再入单，
# 保证之后可被插件重新解析播放，而不是绑一个 30 分钟票据 URL。

_FAV_PLAYLIST: dict = {}


async def ensure_song(item: dict, *, http) -> int:
    """把点播曲目落成 Songloft 库内歌曲，返回 song_id（幂等：dedup_key 去重）。"""
    source = str(item.get("source") or "").strip().lower()
    rid = str(item.get("id") or "").strip()
    if not source or not rid:
        raise SongloftError("ensure_song: item missing source/id")
    title = str(item.get("name") or "").strip()
    if not title:
        raise SongloftError("ensure_song: item missing title")
    artist = str(item.get("artist") or item.get("singer") or "")
    album = str(item.get("album") or "")
    duration = float(item.get("duration_s") or 0)
    source_data = {"s": source, "i": rid, "n": title, "a": artist, "al": album, "d": duration}
    # §2.77-58：保留咪咕 by-id 取链必需的 copyrightId（搜索项 extra）。手机插件写入的
    # 收藏本就带同字段；bridge ensure_song 入库也带上，两条路径形态统一——FAVCLICK
    # rawID 直构才能从 source_data.c 补齐（否则 migu /song/url 404）。
    copyright_id = str((item.get("extra") or {}).get("copyrightId") or "").strip()
    if copyright_id:
        source_data["c"] = copyright_id
    body = [{
        "title": title,
        "artist": artist,
        "album": album,
        "cover_url": str(item.get("cover") or ""),
        "duration": duration,
        "plugin_entry_path": "kwqq",
        "source_data": json.dumps(source_data, ensure_ascii=False, separators=(",", ":")),
        # 去重键以 rawID 为唯一基准（§2.77-48）：source 在手机插件（真实平台名）与
        # CCH 设备上报（KWQQ）间摇摆，带 source 会把同一首歌拆成两条 → 收藏重复。
        "dedup_key": f"kwqq_{rid}",
        "is_video": False,
    }]
    data = await _request(http, "POST", "/api/v1/songs/remote", json_body=body)
    songs = data.get("songs")
    if not isinstance(songs, list) or not songs:
        raise SongloftError("ensure_song: no songs returned")
    try:
        return int(songs[0]["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SongloftError(f"ensure_song: bad song id ({exc})") from exc


async def favorites_playlist_id(*, http) -> int:
    """内置收藏歌单 ID（`built_in` + type=normal；电台收藏是 type=radio，不选）。"""
    cached = _FAV_PLAYLIST.get("id")
    if cached:
        return int(cached)
    data = await _request(http, "GET", "/api/v1/playlists", params={"type": "normal"})
    playlists = data.get("playlists")
    if not isinstance(playlists, list):
        raise SongloftError("favorites: unexpected playlist body")
    for pl in playlists:
        labels = [str(x) for x in (pl.get("labels") or [])]
        if "built_in" in labels and str(pl.get("type") or "normal") == "normal":
            _FAV_PLAYLIST["id"] = int(pl["id"])
            return int(pl["id"])
    raise SongloftError("favorites: built-in playlist not found")


async def writable_playlist_ids(*, http) -> dict:
    """所有可写歌单 {id:int -> name:str}（票 13 增强：playlist_id 入参校验用）。

    口径与 favorites_playlist_id 一致：只认 type=normal（radio=电台收藏，不可写）；
    内置歌单（built_in）同样可写（票 13 已实证对 id=1 POST/DELETE 成功）。
    复用 GET /api/v1/playlists?type=normal，不新建歌单（新建仅手机 App，CCH 不建）。
    """
    data = await _request(http, "GET", "/api/v1/playlists", params={"type": "normal"})
    playlists = data.get("playlists")
    if not isinstance(playlists, list):
        raise SongloftError("playlists: unexpected body")
    out = {}
    for pl in playlists:
        if str(pl.get("type") or "normal") != "normal":
            continue
        try:
            out[int(pl["id"])] = str(pl.get("name") or "")
        except (KeyError, TypeError, ValueError):
            continue
    if not out:
        raise SongloftError("playlists: no writable playlist")
    return out


async def add_to_favorites(song_id: int, *, http, playlist_id=None) -> dict:
    pl = int(playlist_id) if playlist_id is not None else await favorites_playlist_id(http=http)
    return await _request(http, "POST", f"/api/v1/playlists/{pl}/songs",
                          json_body={"song_ids": [int(song_id)]})


async def remove_from_favorites(song_id: int, *, http, playlist_id=None) -> dict:
    pl = int(playlist_id) if playlist_id is not None else await favorites_playlist_id(http=http)
    return await _request(http, "DELETE", f"/api/v1/playlists/{pl}/songs/{int(song_id)}")


# ---- 票 25 数据面：Songloft 歌单 → 原厂收藏 tab item schema -------------------------
# 真机 schema 冻结点 = facts §2.76-4（V2 响应 data.model[] 实测，2026-11-22）。
# 关键口径：UI 列表行曲名取 title、歌手取 authors[].title（singer 恒 null，按 singer 映射会全空）。

async def playlist_songs(playlist_id: int, *, http, limit=200) -> list:
    """读歌单歌曲（票 25：收藏列表下发设备文件用）。

    端点由 Songloft 源码实证：GET /api/v1/playlists/{id}/songs → {songs,total,limit,offset}
    （routers.go:204 / playlist.go:499）。仅取元数据，不含 audioUrl/票据 URL。
    """
    data = await _request(http, "GET", f"/api/v1/playlists/{int(playlist_id)}/songs",
                          params={"limit": int(limit), "offset": 0, "sort": "position", "order": "asc"})
    songs = data.get("songs")
    if not isinstance(songs, list):
        raise SongloftError("playlist_songs: unexpected body")
    return songs


async def lookup_favorite_by_rawid(raw_id: str, *, http=None) -> dict | None:
    """按歌曲唯一身份 rawID 从 Songloft 收藏歌单查回完整元数据（架构统一：
    收藏/历史点击、rawID 直构点播、红心反查、收藏写入都复用这一处，消除 4 份重复循环）。

    Returns:
        {"song_id": int, "title": str, "cover": str, "album": str, "artist": str,
         "copyrightId": str, "source": str} 或 None（未找到/异常）。
    """
    import json as _json
    own = http is None
    if own:
        import aiohttp
        http = aiohttp.ClientSession()
    try:
        pl = await favorites_playlist_id(http=http)
        songs = await playlist_songs(pl, http=http)
        for s in songs:
            sd_raw = s.get("source_data")
            if not isinstance(sd_raw, str) or not sd_raw:
                continue
            try:
                sd = _json.loads(sd_raw)
            except (ValueError, TypeError):
                continue
            if str(sd.get("i") or "") != str(raw_id):
                continue
            return {
                "song_id": int(s.get("id") or 0),
                "title": str(s.get("title") or sd.get("n") or ""),
                "cover": str(s.get("cover_url") or ""),
                "album": str(s.get("album") or sd.get("al") or ""),
                "artist": str(s.get("artist") or sd.get("a") or ""),
                "copyrightId": str(sd.get("c") or ""),
                "source": str(sd.get("s") or ""),
            }
    except Exception:  # noqa: BLE001 - fail-open：反查失败不阻塞点播
        return None
    finally:
        if own:
            await http.close()
    return None


async def favorites_changes(cursor: str | None, *, http) -> dict:
    """收藏变更长轮询（CCH 即时同步；Songloft upstream `favorites/changes`）。

    GET /api/v1/playlists/favorites/changes?cursor=... → {cursor, changed}：
    - 首次不传 cursor / 服务端重启 → 立即 changed=true；
    - 带 cursor 最多挂起 25s，成员提交后唤醒；超时 changed=false。
    cursor 不透明，仅作缓存失效，不做事件重放。返回原始 dict 供纯函数 seam 处理。
    """
    params = {} if not cursor else {"cursor": cursor}
    return await _request(http, "GET", "/api/v1/playlists/favorites/changes",
                          params=params, timeout=_LONG_POLL_TIMEOUT)


def _fav_cover(img: str) -> dict:
    """原厂 cover 壳（实测字段：img 必填，其余可 null）。"""
    i = str(img or "").strip()
    return {"img": i, "large": None, "mediam": None, "medium": None, "small": None,
            "canResize": False, "vertical": None, "horizontal": None,
            "displayRatio": None, "verticalLarge": None, "horizontalLarge": None}


# 门面/插件 source → 平台显示名（§2.77-53，与 Songloft 后端 platform_name、Flutter kSongPlatformNames 对齐）
_PLATFORM_LABEL = {
    "kugou": "酷狗", "netease": "网易云", "qq": "QQ", "kuwo": "酷我",
    "migu": "咪咕", "qianqian": "千千", "bilibili": "哔哩哔哩",
}


def _platform_label(source: str) -> str:
    """平台标识 → 中文名；未知/聚合伪源（kwqq）返回空串。"""
    return _PLATFORM_LABEL.get(str(source or "").strip().lower(), "")


def _fav_title_label(title: str, album: str = "", source: str = "") -> str:
    """收藏 tab 歌名栏 =「歌名-《专辑》-平台」（2026-11-24 用户本轮裁决，取代 §2.77-53
    的「歌手列放组合串」版——真机证实右侧歌手列窄会截断）。组合串放宽栏（歌名列）；
    空专辑省略《》段，未知/伪源省略平台段。"""
    t = str(title or "").strip()
    if not t:
        return ""
    parts = [t]
    al = str(album or "").strip()
    if al:
        parts.append("《%s》" % al)
    plat = _platform_label(source)
    if plat:
        parts.append(plat)
    return "-".join(parts)


def _fav_authors(artist: str, source: str) -> list:
    """原厂 authors[] 壳——UI 的**歌手列取这里**（song.singer 恒 null）。
    本轮裁决：歌手列保持纯歌手（组合串已移到歌名栏 _fav_title_label）。
    """
    a = str(artist or "").strip()
    if not a:
        return []
    return [{"rawId": "", "source": source, "order": 0, "id": 0, "title": a,
             "alias": None, "cleanTitle": None, "description": None,
             "isOnline": True, "type": "program", "isAudition": None,
             "gender": {"name": "DEFAULT"},
             "authorTypes": [{"name": "SINGER"}], "cover": None}]


def song_rawid(song: dict) -> str:
    """从 Songloft 歌曲对象提取 rawID（唯一身份）——favorite_item_schema 与
    历史红心校准（cch_history.load_favorite_rawids）共用，避免两处解析口径漂移。

    source_data.i 为真值；缺失时回退 dedup_key（格式 kwqq_<rawID>，去前缀）。
    无有效 id → 空串（调用方按需跳过）。
    """
    if not isinstance(song, dict):
        return ""
    rid = ""
    try:
        sd = song.get("source_data")
        if isinstance(sd, str) and sd:
            parsed = json.loads(sd)
            if isinstance(parsed, dict):
                rid = str(parsed.get("i") or "").strip()
    except (ValueError, TypeError):
        pass
    if not rid:
        dk = str(song.get("dedup_key") or "").strip()
        rid = dk[5:] if dk.startswith("kwqq_") else dk
    return rid if rid and rid != "0" else ""


def favorite_item_schema(songs: list) -> list:
    """Songloft 歌曲对象 → 原厂「我的收藏」item schema（票 25，纯函数可单测）。

    字段逐项对齐 facts §2.76-4 的真实 V2 响应（不是最小 12 字段版——缺 cover/authors
    会让原厂列表无封面、无歌手）。缺失关键字段宁可跳过该曲，绝不输出空值项。

    凭据纪律（票 25 硬红线 3）：只取元数据；screenPlayUrl 用固定 genie:// 深链 +
    平台/原始 id 拼 contentList（原厂样本同款形态），不读也不下发任何票据 URL。
    """
    out = []
    for pos, song in enumerate(songs or []):
        if not isinstance(song, dict):
            continue
        title = str(song.get("title") or "").strip()
        if not title:
            continue
        # source_data 是 JSON 字符串（ensure_song 写入 {s,i,n,a,al,d}）
        source, raw_id, sd_artist, sd_album = "", "", "", ""
        try:
            sd = song.get("source_data")
            if isinstance(sd, str) and sd:
                parsed = json.loads(sd)
                if isinstance(parsed, dict):
                    source = str(parsed.get("s") or "").strip()
                    raw_id = str(parsed.get("i") or "").strip()
                    sd_artist = str(parsed.get("a") or "").strip()
                    sd_album = str(parsed.get("al") or "").strip()
        except (ValueError, TypeError):
            pass
        if not raw_id:
            # 票 10h：统一走 song_rawid（source_data.i 或 dedup_key 去 kwqq_ 前缀），
            # 与历史红心校准口径一致——避免收藏 tab rawId 带前缀、历史按裸 ID 匹配。
            raw_id = song_rawid(song)
        if not raw_id:
            continue    # 无原始 id 无法逐曲点播，跳过
        try:
            sid = int(song.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0:
            continue
        try:
            duration = int(float(song.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        src = (source or "kwqq").upper()
        artist = str(song.get("artist") or sd_artist or "").strip()
        album = str(song.get("album") or sd_album or "").strip()
        cover = str(song.get("cover_url") or "").strip()
        # 架构统一：原厂 item schema 由 cch_schema 单一实现（收藏/历史共用）
        from core.music.cch_schema import native_item_schema
        out.append(native_item_schema(
            sid=sid, raw_id=raw_id, source=source, title=title,
            artist=artist, album=album, cover=cover, duration=duration, pos=pos,
            collect=True, play_from="playFavorite",
            display_title=_fav_title_label(title, album, source),
            include_album_item=True))
    return out
