#!/usr/bin/env python3
"""音乐库整理迁移脚本（群晖 python3 零依赖）
用法: python3 migrate_music.py [--apply]
  默认 dry-run 只列清单；--apply 实际执行移动
规则:
  源 = /volume1/music 根下音频 + xiaomusic/ + download/ 下音频(排除 tmp/cache)
  艺名推断: ①父目录名(非"其他"等泛目录) ②文件名"艺人 - 歌名"左侧 ③_未分类
  同名 .lrc/.jpg 跟随移动; 目标重名自动加 _N 后缀
"""
import os, re, sys, shutil

ROOT = "/volume1/music"
AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".wma", ".ape", ".dsf"}
SIDE_EXT = {".lrc", ".jpg", ".jpeg", ".png"}
GENERIC_DIRS = {"其他", "download", "cache", "tmp", "未分类", "mp3", "music", "xiaomusic", "KugouMusicClient", "lossless", "inbox"}
EXCLUDE_TOP = {"#snapshot", "@eaDir", "lossless", "mp3", "inbox", "cache", "lms"}

def audio_files():
    """收集待迁移音频: 根下散文件 + xiaomusic/ + download/ 递归"""
    sources = []
    for name in os.listdir(ROOT):
        p = os.path.join(ROOT, name)
        if os.path.isfile(p) and os.path.splitext(name)[1].lower() in AUDIO_EXT:
            sources.append(p)
    for sub in ("xiaomusic", "download"):
        base = os.path.join(ROOT, sub)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("tmp", "cache", "cache_songs", "@eaDir")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                    sources.append(os.path.join(dirpath, fn))
    return sorted(sources)

LOSSLESS_EXT = {".flac", ".dsf", ".ape"}

def dest_root_for(ext):
    return os.path.join(ROOT, "lossless") if ext in LOSSLESS_EXT else os.path.join(ROOT, "mp3")

DISC_RE = re.compile(r"^(CD|Disc|碟)\s*\d+$", re.I)

def guess_artist(path):
    rel = os.path.relpath(path, ROOT)
    parts = rel.split(os.sep)
    # ① 从紧邻父目录向上找第一个有意义的名（跳过 泛目录/碟片号/_开头）
    for i in range(len(parts) - 2, 0, -1):
        cand = parts[i]
        if cand in GENERIC_DIRS or DISC_RE.match(cand) or cand.startswith("_"):
            continue
        return cand
    # ② 文件名 "艺人 - 歌名"
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^(.{1,40}?)\s+-\s+", stem)
    if m:
        return m.group(1).strip()
    return "_未分类"

def unique_dst(dst):
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"

def main():
    apply = "--apply" in sys.argv
    files = audio_files()
    plan, skipped = [], []
    for src in files:
        ext = os.path.splitext(src)[1].lower()
        artist = guess_artist(src)
        artist_dir = os.path.join(dest_root_for(ext), artist)
        dst = unique_dst(os.path.join(artist_dir, os.path.basename(src)))
        plan.append((src, dst))
        # 侧车文件
        sbase = os.path.splitext(src)[0]
        for se in SIDE_EXT:
            side = sbase + se
            if os.path.isfile(side):
                plan.append((side, unique_dst(os.path.join(artist_dir, os.path.basename(side)))))
    # 汇总
    artists = {}
    for s, d in plan:
        zone = d.replace(ROOT + "/", "").split("/")[0]
        a = zone + "/" + os.path.basename(os.path.dirname(d))
        artists[a] = artists.get(a, 0) + 1
    n_ll = sum(1 for s, d in plan if "/lossless/" in d)
    print(f"== 计划迁移 {len(plan)} 个文件（含侧车）：无损 {n_ll} / 有损 {len(plan)-n_ll} ==")
    for a in sorted(artists, key=lambda x: -artists[x])[:15]:
        print(f"   mp3/{a}/  x{artists[a]}")
    print("== 样例(前12) ==")
    for s, d in plan[:12]:
        print(f"  {s.replace(ROOT+'/','')}  ->  {d.replace(ROOT+'/','')}")
    bad = [s for s, d in plan if not os.path.isfile(s)]
    if bad:
        print(f"!! 异常: {len(bad)} 个源文件不存在"); sys.exit(1)
    if not apply:
        print("== DRY-RUN 结束（加 --apply 执行）==")
        return
    n = 0
    for s, d in plan:
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.move(s, d)
        n += 1
    print(f"== 已移动 {n} 个文件 ==")
    # 清理空目录与旧痕迹
    for sub in ("xiaomusic", "download", "tmp", "lms"):
        p = os.path.join(ROOT, sub)
        if os.path.isdir(p):
            try:
                os.removedirs(p)
                print(f"已删除空目录: {sub}/")
            except OSError:
                print(f"目录非空保留: {sub}/")

if __name__ == "__main__":
    main()
