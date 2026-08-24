#!/usr/bin/env python3
"""_未分类 v2: GBK乱码标签修复 + 文件名解析交叉 + 按艺名归档"""
import os, re, sys, shutil
from mutagen.id3 import ID3NoHeaderError, ID3, TIT2, TPE1

BASE = "/app/music/mp3/_未分类"
APPLY = "--apply" in sys.argv
AUDIO = {".mp3"}
SIDE = {".lrc", ".jpg", ".jpeg", ".png"}
PLACEHOLDER = {"", "unknown", "国外", "未知", "网络歌曲", "暂无"}

def unmojibake(s):
    """GBK 字节被误存为 latin-1 的经典乱码还原"""
    if not s:
        return s
    try:
        fixed = s.encode("latin-1").decode("gbk")
        cn = lambda t: sum("\u4e00" <= c <= "\u9fff" for c in t)
        if cn(fixed) > cn(s):
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return s

def parse_fn(stem):
    m = re.match(r"^(.+?)[-–_]\s*(.+)$", stem)
    if m:
        return m.group(1).strip(" ._"), m.group(2).strip(" ._")
    m = re.match(r"^(.{1,30}?)\(([^()]{2,20})\)$", stem)  # 歌名(艺人A 艺人B)
    if m:
        return m.group(2).replace(" ", "/"), m.group(1).strip()
    return None, None

items, keep = [], []
for fn in sorted(os.listdir(BASE)):
    p = os.path.join(BASE, fn)
    if not os.path.isfile(p) or not fn.lower().endswith(".mp3"):
        continue
    stem = os.path.splitext(fn)[0]
    # 读现有标签(可能 GBK 乱码)
    try:
        t = ID3(p)
        raw_a = t.get("TPE1").text[0] if t.get("TPE1") else ""
        raw_t = t.get("TIT2").text[0] if t.get("TIT2") else ""
    except ID3NoHeaderError:
        raw_a = raw_t = ""
    tag_a, tag_t = unmojibake(raw_a), unmojibake(raw_t)
    fn_a, fn_t = parse_fn(stem)

    # 艺名决策: 文件名解析 > 有效标签
    artist = fn_a or (tag_a if tag_a.strip().lower() not in PLACEHOLDER else "")
    # 标题决策: 文件名解析 > 修正标签(剥掉"艺人-"前缀) > 原名
    title = fn_t or tag_t or stem
    m2 = re.match(rf"^{re.escape(artist)}\s*[-–]\s*(.+)$", title) if artist else None
    if m2:
        title = m2.group(1)

    valid = bool(artist) and bool(title)
    (items if valid else keep).append((p, fn, artist, title,
                                       f"tag[{tag_a}|{tag_t}] fn[{fn_a}|{fn_t}]"))

print(f"== 可处理 {len(items)} | 无法确定 {len(keep)} ==")
for p, fn, a, t, src in items:
    print(f"  {fn[:40]:40s} -> [{a}] {t}   ({src[:40]})")
if keep:
    print("-- 无法确定(保持原地):")
    for p, fn, a, t, src in keep:
        print(f"   {fn[:44]}")

if not APPLY:
    print("== DRY-RUN (--apply 执行) ==")
    sys.exit(0)

nok = nfail = 0
for p, fn, artist, title, _ in items:
    try:
        try:
            tags = ID3(p)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("TPE1"); tags.delall("TIT2")
        tags.add(TPE1(encoding=3, text=[artist]))
        tags.add(TIT2(encoding=3, text=[title]))
        tags.update_to_v23()
        tags.save(p, v2_version=3)
    except Exception as e:
        print(f"[FAIL] {fn}: {e}"); nfail += 1; continue
    dst_dir = os.path.join(os.path.dirname(BASE), artist)
    os.makedirs(dst_dir, exist_ok=True)
    def u(d):
        b, e2 = os.path.splitext(d); i = 1
        while os.path.exists(d := f"{b}_{i}{e2}") and (i := i + 1): pass
        return d
    shutil.move(p, u(os.path.join(dst_dir, fn)))
    for se in SIDE:
        side = os.path.splitext(p)[0] + se
        if os.path.isfile(side):
            shutil.move(side, u(os.path.join(dst_dir, os.path.basename(side))))
    nok += 1
print(f"== 完成: 成功 {nok} / 失败 {nfail} ==")
