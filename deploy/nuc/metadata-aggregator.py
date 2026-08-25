#!/usr/bin/env python3
"""
metadata-aggregator.py — 多源音乐元数据聚合引擎 v1.0

并行查询多个公开数据源，按字段级择优策略合并，
输出统一 JSON 元数据供 CUE 生成和 FLAC 标签更新使用。

数据源（按字段擅长领域分工）:
  MusicBrainz : 古典作曲家/作品/结构化最好
  Discogs     : 实体发行(厂牌/目录号)、爵士/流行收藏价值
  Deezer      : 现代曲目、高清封面 URL
  iTunes      : 流派分类规范、商业标准命名
  ncm-api     : 中文流行曲库(用户已部署)
  kugou-api   : 中文曲库补充(用户已部署)

用法:
  python3 metadata-aggregator.py --album "专辑名" [--artist "演奏者"] [--output meta.json]
  python3 metadata-aggregator.py --dir <含flac的本地目录> [--output meta.json]
"""
import json, os, sys, re, argparse, urllib.request, urllib.parse, time, concurrent.futures

# ===== 配置 =====
NCM_API = "http://10.10.10.2:3000"
KUGOU_API = "http://10.10.10.2:3001"
KWQQ_API = "http://10.10.10.2:3003"
UA = "NUC-HIFI-MetadataAggregator/1.0"
TIMEOUT = 12

def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ===== 各源查询器 =====

def query_musicbrainz(album, artist=""):
    q = urllib.parse.quote(album)
    if artist: q += f"+ AND artist:{urllib.parse.quote(artist)}"
    url = f"https://musicbrainz.org/ws/2/release/?query={q}&fmt=json&limit=3"
    d = http_get_json(url)
    releases = d.get("releases", [])
    if not releases: return None
    r = releases[0]  # 最高分
    
    tracks = []
    for m in r.get("media", []):
        for t in m.get("tracks", []):
            rec = t.get("recording", {})
            acs = rec.get("artist-credit", [])
            performers = ", ".join(
                ac.get("name","") for ac in acs if isinstance(ac, dict) and ac.get("joinphrase","") != "/"
            )
            
            # 提取 composer (从 relations 或 work)
            composer = ""
            for rel in rec.get("relations", []):
                if rel.get("type") == "performance":
                    work = rel.get("work", {})
                    for wrel in work.get("relations", []):
                        if wrel.get("type") == "composer":
                            composer = wrel.get("artist", {}).get("name", "")
                            break
            
            tracks.append({
                "number": t.get("number", ""),
                "title": rec.get("title") or t.get("title", ""),
                "performer": performers,
                "composer": composer,
                "duration": t.get("length", 0) / 1000 if t.get("length") else 0,
            })
    
    return {
        "source": "MusicBrainz",
        "album": r.get("title", ""),
        "date": r.get("date", ""),
        "country": r.get("country", ""),
        "barcode": r.get("barcode", ""),
        "label": next((l["label"]["name"] for li in r.get("label-info", []) 
                       if isinstance(li, dict) and li.get("label")), ""),
        "tracks": tracks,
    }


def query_discogs(album, artist=""):
    q = urllib.parse.quote(f"{album} {artist}".strip())
    url = f"https://api.discogs.com/database/search?q={q}&type=release&per_page=3"
    d = http_get_json(url)
    results = d.get("results", [])
    if not results: return None
    r = results[0]
    
    # 获取详情以拿到 tracklist
    detail_url = f"https://api.discogs.com/releases/{r['id']}"
    detail = http_get_json(detail_url)
    
    tracks = []
    for t in detail.get("tracklist", []):
        if t.get("type_") == "Track":
            artists_str = ", ".join(a.get("name","") for a in t.get("artists", []))
            tracks.append({
                "number": t.get("position", ""),
                "title": t.get("title", ""),
                "performer": artists_str,
                "duration": _parse_discogs_duration(t.get("duration", "")),
            })
    
    return {
        "source": "Discogs",
        "album": detail.get("title", r.get("title", "")),
        "date": r.get("year", ""),
        "country": r.get("country", ""),
        "barcode": next((i.get("value","") for i in r.get("identifiers",[]) 
                         if i.get("type")=="Barcode"), ""),
        "label": next((l.get("name","") for l in r.get("labels",[])), ""),
        "catno": next((l.get("catno","") for l in r.get("labels",[])), ""),
        "genres": r.get("genre", []),
        "styles": r.get("style", []),
        "cover_url": r.get("cover_image", ""),
        "tracks": tracks,
    }

def _parse_discogs_duration(d):
    """'3:45' → 225.0"""
    try:
        parts = [int(x) for x in d.split(":")]
        if len(parts) == 2: return parts[0]*60 + parts[1]
        if len(parts) == 3: return parts[0]*3600 + parts[1]*60 + parts[2]
    except: pass
    return 0


def query_deezer(album, artist=""):
    q = urllib.parse.quote(f"{album} {artist}".strip())
    url = f"https://api.deezer.com/search/album?q={q}&limit=1"
    d = http_get_json(url)
    data = d.get("data", [])
    if not data: return None
    a = data[0]
    
    # 获取 tracks
    tracklist_url = a.get("tracklist", "")
    tracks = []
    if tracklist_url:
        td = http_get_json(tracklist_url)
        for t in td.get("data", []):
            tracks.append({
                "number": str(t.get("track_position", "")),
                "title": t.get("title_short") or t.get("title", ""),
                "performer": t.get("artist", {}).get("name", ""),
                "duration": t.get("duration", 0),
            })
    
    return {
        "source": "Deezer",
        "album": a.get("title", ""),
        "cover_url": a.get("cover_xl") or a.get("cover_big", ""),
        "genres": [g.get("name","") for g in a.get("genres", {}).get("data", [])],
        "tracks": tracks,
    }


def query_itunes(album, artist=""):
    q = urllib.parse.quote(f"{album} {artist}".strip())
    url = f"https://itunes.apple.com/search?term={q}&entity=album&limit=1"
    d = http_get_json(url)
    results = d.get("results", [])
    if not results: return None
    r = results[0]
    return {
        "source": "iTunes",
        "album": r.get("collectionName", ""),
        "genre": r.get("primaryGenreName", ""),
        "date": r.get("releaseDate", "")[:10],
        "track_count": r.get("trackCount", 0),
        "cover_url": r.get("artworkUrl100", "").replace("100x100", "600x600"),
    }


def query_ncm(keyword):
    """通过用户已有的 ncm-api 搜索网易云(中文曲库强项)"""
    url = f"{NCM_API}/search?keywords={urllib.parse.quote(keyword)}&limit=5&type=1"
    d = http_get_json(url)
    songs = d.get("result", {}).get("songs", [])
    if not songs: return None
    return [{
        "id": s.get("id"),
        "title": s.get("name", ""),
        "artist": ", ".join(a.get("name","") for a in s.get("artists", [])),
        "album": s.get("album", {}).get("name", ""),
    } for s in songs[:5]]


# ===== 合并引擎 =====

FIELD_PRIORITY = {
    "album":       ["MusicBrainz", "Discogs", "iTunes", "Deezer"],
    "date":        ["MusicBrainz", "Discogs", "iTunes"],
    "country":     ["MusicBrainz", "Discogs"],
    "label":       ["Discogs", "MusicBrainz"],
    "catno":       ["Discogs"],
    "barcode":     ["MusicBrainz", "Discogs"],
    "genre":       ["iTunes", "Discogs"],
    "cover_url":   ["Deezer", "iTunes"],
}

def merge_sources(sources: list) -> dict:
    """字段级择优合并"""
    merged = {}
    
    # 取第一个有值的源的字段
    for field, priority_list in FIELD_PRIORITY.items():
        for src_name in priority_list:
            for src in sources:
                if src and src.get("source") == src_name:
                    val = src.get(field)
                    if val and str(val).strip():
                        merged[field] = str(val).strip()
                        merged[f"{field}_source"] = src_name
                        break
            else:
                continue
            break
    
    # 曲目合并：优先用 track 数最多的源
    best_tracks_src = max(
        (s for s in sources if s and s.get("tracks")),
        key=lambda s: len(s["tracks"]),
        default=None
    )
    if best_tracks_src:
        merged["tracks"] = best_tracks_src["tracks"]
        merged["tracks_source"] = best_tracks_src["source"]
        merged["total_tracks"] = len(best_tracks_src["tracks"])
    
    merged["sources_queried"] = [s.get("source") for s in sources if s]
    merged["merge_timestamp"] = __import__("datetime").datetime.now().isoformat()
    
    return merged


# ===== 主流程 =====

def aggregate(album, artist=""):
    """并行查询所有源并合并"""
    print(f"🔍 聚合搜索: album='{album}' artist='{artist}'")
    
    sources = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            "MusicBrainz": pool.submit(query_musicbrainz, album, artist),
            "Discogs": pool.submit(query_discogs, album, artist),
            "Deezer": pool.submit(query_deezer, album, artist),
            "iTunes": pool.submit(query_itunes, album, artist),
        }
        
        for name, future in futures.items():
            try:
                result = future.result(timeout=TIMEOUT + 5)
                if result:
                    sources.append(result)
                    track_count = len(result.get("tracks", []))
                    print(f"  [{name}] ✓ {track_count} tracks")
                else:
                    print(f"  [{name}] 无结果")
            except Exception as e:
                print(f"  [{name}] error: {e}")
    
    if not sources:
        return {}
    
    merged = merge_sources(sources)
    merged["_raw_sources"] = {s["source"]: s for s in sources if s}
    return merged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="多源音乐元数据聚合")
    parser.add_argument("--album", required=True, help="专辑名")
    parser.add_argument("--artist", default="", help="演奏者/歌手")
    parser.add_argument("--output", default="", help="输出 JSON 路径")
    args = parser.parse_args()
    
    result = aggregate(args.album, args.artist)
    
    output_path = args.output or "/tmp/music-meta.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n📊 结果:")
    print(f"  专辑: {result.get('album','?')}")
    print(f"  年代: {result.get('date','?')} ({result.get('date_source','')})")
    print(f"  厂牌: {result.get('label','?')} ({result.get('label_source','')})")
    print(f"  曲目: {result.get('total_tracks',0)} 首 ({result.get('tracks_source','')})")
    print(f"  来源: {', '.join(result.get('sources_queried',[]))}")
    print(f"\n  已保存: {output_path}")
