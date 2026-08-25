#!/usr/bin/env bash
# ============================================================
# music-pipeline.sh v2.0 — 音乐入库流水线
# 功能：APE→FLAC / CUE切分 / 封面核对 / Booklet瘦身 / 自动入库
#
# 前提：NUC(root) + NAS 免密(ssh nas) + 工具链(flac/shntool/cuetools/ffmpeg/gs)
# 用法：
#   bash music-pipeline.sh <NAS源目录路径> [分类名,默认classical]
# 示例：
#   bash music-pipeline.sh "/volume1/TEMP/inbox/EMI 世纪典藏 (10+1CD)" classical
#
# 处理逻辑：
#   每个子目录 = 一个专辑/演出
#   ├─ 整轨 APE + CUE → 解码→切分→FLAC→标签→规范命名
#   ├─ 分轨 APE（无 CUE）→ 逐文件转 FLAC
#   ├─ 已有 FLAC → 跳过转换
#   ├─ 封面核对补齐 → cover.jpg
#   ├─ PDF booklet → gs 瘦身 → booklets/<专辑>.pdf
#   └─ APE 归档 → archive_ape/<专辑>/
#
# 输出：lossless/<分类>/<专辑>/ 分轨 FLAC + cover.jpg
# 触发：LMS rescan + xiaomusic 刷新
# ============================================================
set -uo pipefail

# ===== 配置 =====
NAS_ALIAS="nas"                       # ~/.ssh/config 别名
CATEGORY="${2:-classical}"
LOSSLESS_REMOTE="/volume1/music/lossless/$CATEGORY"
BOOKLET_REMOTE="/volume1/music/booklets"
ARCHIVE_REMOTE="/volume1/music/archive_ape"
WORK="/var/tmp/music-pipeline"
PARALLEL=2

PASS=0; FAIL=0; SKIP=0
ok()   { echo "  [✅] $*"; PASS=$((PASS+1)); }
ng()   { echo "  [❌] $*" | tee /dev/stderr; FAIL=$((FAIL+1)); }
skip() { echo "  [⏭] $*"; SKIP=$((SKIP+1)); }

echo "=============================================="
echo " 音乐入库流水线 v2.0"
echo " 源: $1"
echo " 分类: $CATEGORY"
echo "=============================================="

SRC_DIR=$(realpath "$1")
[[ -d "$SRC_DIR" ]] || { ng "源目录不存在: $SRC_DIR"; exit 1; }

# ===== 工具函数 =====

# 判断整轨 vs 分轨
# 返回: "whole" / "tracks" / "flac_only" / "empty"
classify_dir() {
    local d="$1"
    local n_ape=0 n_flac=0 n_cue=0 biggest=0
    for f in "$d"/*; do
        [[ -f "$f" ]] || continue
        case "${f,,}" in
            *.ape)
                ((n_ape++))
                sz=$(stat -c%s "$f" 2>/dev/null); [[ $sz -gt $biggest ]] && biggest=$sz ;;
            *.flac) ((n_flac++)) ;;
            *.cue)  ((n_cue++)) ;;
        esac
    done
    if [[ $n_ape -eq 1 && $n_cue -ge 1 ]]; then echo "whole"
    elif [[ $n_ape -gt 1 ]]; then echo "tracks"
    elif [[ $n_flac -gt 0 ]]; then echo "flac_only"
    else echo "empty"; fi
}

# 找封面文件（按优先级）
find_cover() {
    local d="$1"
    for name in cover.jpg folder.jpg front.jpg Cover.jpg Cover\ Front.jpg albumart.jpg; do
        [[ -f "$d/$name" ]] && { echo "$d/$name"; return; }
    done
    # 取最大的 jpg 作为封面
    local best="" best_sz=0
    for f in "$d"/*.jpg "$d"/*.jpeg; do
        [[ -f "$f" ]] || continue
        sz=$(stat -c%s "$f" 2>/dev/null)
        if [[ ${sz:-0} -gt $best_sz ]]; then best="$f"; best_sz=$sz; fi
    done
    echo "$best"
}

# 从 cue 提取专辑名
get_cue_album() {
    local cue="$1"
    grep -m1 '^TITLE' "$cue" 2>/dev/null | sed 's/^TITLE[[:space:]]*"//;s/"$//' | head -c 80
}

# 单个 APE → FLAC（保留标签）
convert_ape_to_flac() {
    local ape="$1"
    local flac="${ape%.ape}.flac"
    [[ -f "$flac" ]] && return 0
    ffmpeg -y -loglevel error -i "$ape" -map_metadata 0 "$flac" 2>/dev/null
    [[ -f "$flac" ]]
}

# 整轨 APE+CUE → 分轨 FLAC
split_whole_track() {
    local dir="$1"
    local ape=$(find "$dir" -maxdepth 1 -iname "*.ape" | head -1)
    local cue=$(find "$dir" -maxdepth 1 -iname "*.cue" | head -1)
    local tmp
    tmp=$(mktemp -d /var/tmp/split-XXXXXX)

    # 解码
    log "  解码 $(basename "$ape")..."
    ffmpeg -y -loglevel error -i "$ape" "$tmp/audio.wav"

    # 替换 cue 的 FILE 引用
    sed "s|FILE \"[^\"]*\"|FILE \"$tmp/audio.wav\"|" "$cue" > "$tmp/split.cue"

    # 切分 + flac 编码（shntool 内建 flac 输出）
    cd "$tmp"
    shnsplit -f split.cue -o flac -t "%n. %t" audio.wav 2>&1 | tail -1

    # 标签注入
    cuetag.sh split.cue *.flac 2>/dev/null

    # 移入原目录
    rm -f "$dir"/*.flac   # 清掉旧的整轨 flac（如有）
    mv *.flac "$dir/"

    cd /
    rm -rf "$tmp"
}

# ===== 主流程 =====

# Phase 1: rsync 拉取到本地（避免网络文件系统 IO 问题）
log "Phase 1: 拉取源数据到本地..."
mkdir -p "$WORK"
rsync -a --info=progress2 \
    "${NAS_ALIAS}:${SRC_DIR}/" "${WORK}/source/" 2>&1 | tail -1
[[ -d "$WORK/source" ]] || { ng "拉取失败"; exit 1; }
ok "数据已拉取"

# Phase 2: 遍历子目录处理
log "Phase 2: 处理专辑目录..."

# 收集所有含音频的子目录（含根下散文件视为一个虚拟目录）
declare -A albums
for d in "$WORK"/source/*/; do
    [[ -d "$d" ]] || continue
    has_audio=false
    for f in "$d"*; do
        case "${f,,}" in *.ape|*.flac|*.wav) has_audio=true; break;; esac
    done
    $has_audio && albums["$(basename "$d")"]="$d"
done
# 根下散文件
root_audio=$(find "$WORK/source" -maxdepth 1 -type f \( -iname "*.ape" -o -iname "*.flac" \) | wc -l)
if [[ $root_audio -gt 0 ]]; then
    mkdir -p "$WORK/source/_root_files"
    find "$WORK/source" -maxdepth 1 -type f \( -iname "*.ape" -o -iname "*.flac" \) -exec mv {} "$WORK/source/_root_files/" \;
    albums["_root_files"]="$WORK/source/_root_files"
fi

for album_name in "${!albums[@]}"; do
    album_dir="${albums[$album_name]}"
    log "--- 专辑: $album_name ---"
    
    ftype=$(classify_dir "$album_dir")
    log "  类型: $ftype"
    
    # 确定输出目录名（优先用 cue TITLE）
    out_name="$album_name"
    for cue in "$album_dir"/*.cue; do
        [[ -f "$cue" ]] && { t=$(get_cue_album "$cue"); [[ -n "$t" ]] && out_name="$t"; break; }
    done
    
    dest_dir="${LOSSLESS_REMOTE}/${out_name}"
    
    case "$ftype" in
        whole)
            log "  整轨模式: 解码→切分→编码"
            
            # 在本地执行切分
            ape=$(find "$album_dir" -maxdepth 1 -iname "*.ape" | head -1)
            cue=$(find "$album_dir" -maxdepth 1 -iname "*.cue" | head -1)
            tmp=$(mktemp -d)
            
            ffmpeg -y -loglevel error -i "$ape" "$tmp/audio.wav"
            sed "s|FILE \"[^\"]*\"|FILE \"$tmp/audio.wav\"|" "$cue" > "$tmp/split.cue"
            
            cd "$tmp"
            shnsplit -f split.cue -o flac -t "%n. %t" audio.wav 2>&1 | tail -1
            cuetag.sh split.cue *.flac 2>/dev/null
            
            # 推送到 NAS
            ssh $NAS_ALIAS "mkdir -p '$dest_dir'"
            rsync -a --chmod=D755,F644 ./*.flac "${NAS_ALIAS}:${dest_dir}/"
            
            cd /; rm -rf "$tmp"
            ok "整轨切分完成 ($(find "$tmp" -name "*.flac" 2>/dev/null | wc -l) tracks -> NAS)"
            ;;
        tracks)
            log "  分轨模式: 逐文件 APE→FLAC"
            fail_count=0
            for ape in "$album_dir"/*.ape; do
                [[ -f "$ape" ]] || continue
                flac="${ape%.ape}.flac"
                if [[ ! -f "$flac" ]]; then
                    ffmpeg -y -loglevel error -i "$ape" -map_metadata 0 "$flac" || ((fail_count++))
                fi
            done
            [[ $fail_count -eq 0 ]] && ok "分轨转换完成" || ng "有 $fail_count 个失败"
            # 推送 FLAC 到 NAS
            ssh $NAS_ALIAS "mkdir -p '$dest_dir'"
            rsync -a --chmod=D755,F644 "$album_dir"/*.flac "${NAS_ALIAS}:${dest_dir}/" &&
              ok "推送到 NAS 完成" || ng "推送失败"
            ;;
        flac_only)
            skip "已是 FLAC，无需转换"
            ssh $NAS_ALIAS "mkdir -p '$dest_dir'"
            rsync -a --chmod=D755,F644 "$album_dir"/*.flac "${NAS_ALIAS}:${dest_dir}/" 2>/dev/null
            ;;
        empty)
            skip "无音频文件"
            ;;
    esac
    
    # 封面核对补齐
    cover=$(find_cover "$album_dir")
    if [[ -n "$cover" ]]; then
        ok "封面: $(basename "$cover")"
        # 推送封面为 cover.jpg
        ssh $NAS_ALIAS "mkdir -p '$dest_dir'" 2>/dev/null
        scp -o BatchMode=yes -o UserKnownHostsFile=/dev/null \
            -o StrictHostKeyChecking=no "$cover" \
            "${NAS_ALIAS}:${dest_dir}/cover.jpg" 2>/dev/null &&
          ok "封面已推送" || ng "封面推送失败"
    else
        ng "未找到封面图片"
    fi
    
    # Booklet PDF 瘦身
    for pdf in "$album_dir"/*.pdf; do
        [[ -f "$pdf" ]] || continue
        pdf_name=$(basename "$pdf" .pdf)
        slimmed="/tmp/${pdf_name}_slim.pdf"
        
        gs -sDEVICE=pdfwrite -dPDFSETTINGS="$GS_DPI" \
           -o "$slimmed" "$pdf" 2>/dev/null
        
        orig_sz=$(stat -c%s "$pdf"); slim_sz=$(stat -c%s "$slimmed" 2>/dev/null || echo 0)
        if [[ $slim_sz -gt 0 ]]; then
            # 推送到 NAS booklets 目录
            ssh $NAS_ALIAS "mkdir -p '$BOOKLET_REMOTE'" 2>/dev/null
            scp -o BatchMode=yes -o UserKnownHostsFile=/dev/null \
                -o StrictHostKeyChecking=no "$slimmed" \
                "${NAS_ALIAS}:${BOOKLET_REMOTE}/${out_name}.pdf" 2>/dev/null &&
              ok "booklet 瘦身: $(($orig_sz/1024))KB -> $($slim_sz/1024)KB" ||
              ng "booklet 推送失败"
        else
            ng "gs 瘦身失败: $pdf"
        fi
        rm -f "$slimmed"
    done
done

# Phase 3: APE 归档（保留母带）
log "Phase 3: APE 归档..."
ssh $NAS_ALIAS "mkdir -p '$ARCHIVE_REMOTE'" 2>/dev/null
for d in "$WORK"/source/*/; do
    [[ -d "$d" ]] || continue
    album=$(basename "$d")
    find "$d" -iname "*.ape" | while read ape; do
        rel="${ape#$d}"
        ssh $NAS_ALIAS "mkdir -p '$ARCHIVE_REMOTE/$album/$(dirname "$rel")'" 2>/dev/null
        scp -o BatchMode=yes -o UserKnownHostsFile=/dev/null \
            -o StrictHostKeyChecking=no "$ape" \
            "${NAS_ALIAS}:${ARCHIVE_REMOTE}/${album}/${rel}" 2>/dev/null
    done
done
ok "APE 归档至 $ARCHIVE_REMOTE/"

# Phase 4: 触发 LMS 重扫 + xiaomusic 刷新
log "Phase 4: 触发重扫..."
python3 <<PYEOF
import socket
try:
    s = socket.create_connection(("10.10.10.2", 9090), timeout=8)
    s.sendall(b"wipedb 1\nrescan\n"); s.close()
    print("[OK] LMS rescan triggered")
except Exception as e:
    print(f"[NG] LMS: {e}")
PYEOF

curl -s -m 10 -X POST http://10.10.10.2:8090/cmd \
    -H "Content-Type: application/json" \
    -d '{"did":"566642283","cmd":"刷新列表"}' > /dev/null &&
    ok "xiaomusic 列表已刷新" || ng "xiaomusic 刷新失败"

# ===== 清理工作区 =====
read -rp "清理本地工作区 $WORK? [y/N] " yn
[[ "$yn" == "y" ]] && rm -rf "$WORK" && echo "已清理"

# ===== 汇总 =====
echo ""
echo "=============================================="
echo " 流水线完成"
echo " ✅ 成功: $PASS  ❌ 失败: $FAIL  ⏭ 跳过: $SKIP"
echo " 分类: $CATEGORY"
echo " NAS 目标: $LOSSLESS_REMOTE/"
echo "=============================================="
exit "$FAIL"
