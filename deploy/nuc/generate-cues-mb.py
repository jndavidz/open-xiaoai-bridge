#!/usr/bin/env python3
"""
generate-cues-mb.py — 从 MusicBrainz 数据生成全部 CUE + 修复 FLAC 标签
数据源: MusicBrainz release (完整 11 disc tracklist) + 本地 FLAC 时长
产出: 每张 CD 目录一个 CUE 文件 + FLAC 标签补全
"""
import json, os, re, subprocess, sys

WORK = "/var/tmp/music-work/EMI 世纪典藏 (10+1CD)"
MB_JSON = "/tmp/mb-release.json"

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout.strip()

def get_duration(f):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", f],
        capture_output=True, text=True).stdout.strip()
    try: return float(out)
    except: return 0.0

def read_tags(f):
    tags = {}
    out = subprocess.run(
        ["metaflac", "--export-tags-to=-", f],
        capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            tags[k.upper()] = v.strip()
    return tags

def write_tags(f, tags: dict):
    for k, v in tags.items():
        if v:
            subprocess.run(["metaflac", "--remove-tag=k" if False else "--set-tag", f"{k}={v}", f],
                           capture_output=True)

def secs_to_cue(secs):
    total_frames = int(round(secs * 75))
    mm = total_frames // (60 * 75)
    ss = (total_frames % (60 * 75)) // 75
    ff = total_frames % 75
    return f"{mm:02d}:{ss:02d}:{ff:02d}"


# ===== 加载 MusicBrainz 数据 =====
mb = json.load(open(MB_JSON))
media = mb.get("media", [])
print(f"MusicBrainz: {mb['title']} — {len(media)} discs")

# ===== 遍历各 CD 目录 =====
cd_dirs = sorted(
    [d for d in os.listdir(WORK) if d.startswith("CD") and os.path.isdir(os.path.join(WORK, d))],
    key=lambda x: int(re.search(r'\d+', x).group())
)

total_cues = 0
total_tags_fixed = 0

for cd_idx, cd_name in enumerate(cd_dirs):
    cd_path = os.path.join(WORK, cd_name)
    if cd_idx >= len(media): break
    medium = media[cd_idx]
    mb_tracks = medium.get("tracks", [])

    flacs = sorted([f for f in os.listdir(cd_path) if f.lower().endswith(".flac")])
    
    print(f"\n--- {cd_name}: {len(flacs)} local / {len(mb_tracks)} MB tracks ---")
    
    if len(flacs) != len(mb_tracks):
        print(f"  ⚠ 数量不匹配! 用 min({len(flacs)}, {len(mb_tracks)}) 处理")

    # ===== 1. 更新 FLAC 标签(从 MusicBrainz 补全) =====
    tags_fixed = 0
    for i, flac_name in enumerate(flacs):
        if i >= len(mb_tracks): break
        mt = mb_tracks[i]
        rec = mt.get("recording", {})
        
        mb_title = rec.get("title") or mt.get("title") or ""
        mb_artist = ""
        acs = rec.get("artist-credit", [])
        if acs:
            mb_artist = ", ".join(ac.get("name","") for ac in acs if isinstance(ac, dict))
        
        path = os.path.join(cd_path, flac_name)
        current = read_tags(path)
        
        updates = {}
        if not current.get("TITLE") and mb_title:
            updates["TITLE"] = mb_title
        if (not current.get("ARTIST") or current.get("ARTIST") == "Various Artists") and mb_artist:
            # 合辑保留 Various 但加 PERFORMER
            updates["PERFORMER"] = mb_artist
        if mb_title and "Track" in flac_name:
            updates["TITLE"] = mb_title
        
        if updates:
            for k, v in updates.items():
                subprocess.run(["metaflac", "--set-tag", f"{k}={v}", path], capture_output=True)
            tags_fixed += 1
    
    if tags_fixed:
        print(f"  [✅] {tags_fixed} 个标签已修复")
    
    # ===== 2. 生成 CUE =====
    cue_lines = [
        'REM COMMENT "MusicBrainz + ffprobe pipeline v2.1"',
        f'REM DATE {mb.get("date","1997")}',
        f'TITLE "{mb.get("title","Centenary Edition")} {cd_name}"',
        'PERFORMER "Various Artists"',
        '',
    ]
    
    cumulative = 0.0
    for i, flac_name in enumerate(flacs):
        if i >= len(mb_tracks): break
        mt = mb_tracks[i]
        rec = mt.get("recording", {})
        
        title = rec.get("title") or mt.get("title") or ""
        acs = rec.get("artist-credit", [])
        performer = ", ".join(
            ac.get("name","") for ac in acs 
            if isinstance(ac, dict) and ac.get("joinphrase","") != "/"
        ) or ""
        
        # 截断过长名称
        if len(title) > 80: title = title[:77] + "..."
        if len(performer) > 60: performer = performer[:57] + "..."
        
        flac_path = os.path.join(cd_path, flac_name)
        duration = get_duration(flac_path)
        cue_time = secs_to_cue(cumulative)
        
        cue_lines.append(f'FILE "{flac_name}" WAVE')
        cue_lines.append(f'  TRACK {i+1:02d} AUDIO')
        if title: cue_lines.append(f'    TITLE "{title}"')
        if performer: cue_lines.append(f'    PERFORMER "{performer}"')
        cue_lines.append(f'    INDEX 01 {cue_time}')
        cue_lines.append('')
        
        cumulative += duration
    
    cue_file = os.path.join(cd_path, f"{cd_name}.cue")
    with open(cue_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(cue_lines) + '\n')
    
    print(f"  [✅] CUE 生成: {cue_file} ({len(flacs)} tracks)")
    total_cues += 1

print(f"\n===== 完成: {total_cues} 个 CUE, {total_tags_fixed} 个标签修复 =====")
