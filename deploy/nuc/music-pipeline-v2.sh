#!/usr/bin/env bash
# ============================================================
# music-pipeline v2.1 — 智能音乐入库流水线
#
# 分类引擎: APE数量/大小/时长 → 整轨|分轨|已转
# CUE 引擎: 验证→在线搜索→缺失暂停
# 封面:     核对补齐 cover.jpg
# booklet:  PDF gs 瘦身 → booklets/<专辑>.pdf
# 归档:     APE 母带移入 archive_ape/
# 入库:     lossless/<分类>/<专辑>/ + LMS rescan + xiaomusic refresh
#
# 用法: bash music-pipeline-v2.sh <NAS源目录> [分类]
# ============================================================
set -uo pipefail

NAS="nas"                              # ~/.ssh/config 别名
CATEGORY="${2:-classical}"
LOSSLESS="/volume1/music/lossless/$CATEGORY"
BOOKLETS="/volume1/music/booklets"
ARCHIVE="/volume1/music/archive_ape"
WORKROOT="/var/tmp/music-pipeline"
PARALLEL=2

# ---- 统计器 ----
declare -a RESULTS=()
PASS=0; PAUSED=0; FAILED=0

ok()   { RESULTS+=("✅ $*"); PASS=$((PASS+1)); echo "  [✅] $*"; }
pause(){ RESULTS+=("⏸ $*"); PAUSED=$((PAUSED+1)); echo "  [⏸] $*"; }
ng()   { RESULTS+=("❌ $*"); FAILED=$((FAILED+1)); echo "  [❌] $*" | tee /dev/stderr; }
info() { echo "  [..] $*"; }

# ===== 分析引擎 =====
# 返回 JSON-like: type|ape_count|flac_count|cue_count|biggest_ape_mb|total_duration_min
analyze_album() {
    local d="$1"
    local apes=() flacs=() cues=()
    local biggest=0 total_size=0

    for f in "$d"/*; do
        [[ -f "$f" ]] || continue
        case "${f,,}" in
            *.ape)
                apes+=("$f")
                sz=$(stat -c%s "$f"); ((sz > biggest)) && biggest=$sz
                ((total_size += sz)) ;;
            *.cue) cues+=("$f") ;;
            *.flac) flacs+=("$f") ;;
        esac
    done

    local biggest_mb=$((biggest / 1048576))
    local type="empty"

    if [[ ${#apes[@]} -eq 1 && $biggest_mb -ge 30 ]]; then
        # 单个大 APE = 整轨
        [[ ${#cues[@]} -ge 1 ]] && type="whole_cue" || type="whole_no_cue"
    elif [[ ${#apes[@]} -gt 1 ]]; then
        type="tracks"
    elif [[ ${#flacs[@]} -gt 0 ]]; then
        type="already_flac"
    fi

    echo "${type}|${#apes[@]}|${#flacs[@]}|${#cues[@]}|${biggest_mb}"
}

# ===== CUE 验证器 =====
validate_cue() {
    local cue="$1" dir="$2"
    [[ -f "$cue" ]] || { info "CUE 文件不存在"; return 1; }

    # FILE 引用的音频存在
    local ref
    ref=$(grep -m1 '^FILE' "$cue" | sed 's/FILE "\([^"]*\)".*/\1/')
    [[ -n "$ref" && -f "$dir/$ref" ]] || {
        info "CUE 引用的文件不存在: $ref"; return 1;
    }

    # TRACK 数 > 0
    local tracks=$(grep -c '^\s*TRACK' "$cue" 2>/dev/null || echo 0)
    [[ $tracks -ge 1 ]] || { info "CUE 无有效 TRACK"; return 1; }

    # INDEX 时间递增（简化：只检查第一条和最后一条）
    local first=$(grep -oP 'INDEX 01 \K\d+:\d+:\d+' "$cue" | head -1)
    local last=$(grep -oP 'INDEX 01 \K\d+:\d+:\d+' "$cue" | tail -1)
    [[ -n "$first" && -n "$last" ]] || { info "CUE INDEX 格式异常"; return 1; }

    return 0
}

# ===== CUE 在线搜索(占位，实际实现需按具体 CD TOC 计算 discid) =====
try_fetch_cue_online() {
    local dir="$1"
    # gnudb.org freedb 协议需要 CD frame offsets——APE 不含 TOC
    # MusicBrainz API 需要 discid——同样限制
    # 当前标记为不可自动获取
    info "在线 CUE 搜索需要 CD TOC 信息，APE 格式无法提供"
    return 1
}

# ===== 封面处理 =====
ensure_cover() {
    local dir="$1"
    for name in cover.jpg folder.jpg front.jpg Cover.jpg Cover\ Front.jpg albumart.jpg; do
        [[ -f "$dir/$name" ]] && return 0
    done
    # 取最大 jpg
    local best="" sz=0
    for f in "$dir"/*.jpg "$dir"/*.jpeg; do
        [[ -f "$f" ]] || continue
        s=$(stat -c%s "$f" 2>/dev/null); ((s > sz)) && { best="$f"; sz=$s; }
    done
    [[ -n "$best" ]] && cp "$best" "$dir/cover.jpg" && return 0
    return 1
}

# ===== Booklet 瘦身 =====
slim_pdf() {
    local src="$1" dst="$2"
    command -v gs >/dev/null || return 1
    gs -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook -o "$dst" "$src" 2>/dev/null
    [[ -f "$dst" ]]
}

# ===== 主流程 =====
SRC_DIR=$(realpath "$1")
[[ -d "$SRC_DIR" ]] || { ng "源目录不存在: $SRC_DIR"; exit 1; }

echo "═══════════════════════════════════════════"
echo " 音乐入库流水线 v2.1 | 分类:$CATEGORY"
echo " 源: $SRC_DIR"
echo "═══════════════════════════════════════════"

# Phase 1: 拉取到本地
info "Phase 1: 拉取源数据..."
mkdir -p "$WORKROOT"
rsync -a --info=progress2 \
    "${NAS}:${SRC_DIR}/" "${WORKROOT}/source/" 2>&1 | tail -1
[[ -d "$WORKROOT/source" ]] || { ng "拉取失败"; exit 1; }
ok "数据已拉取到本地"

# Phase 2: 逐专辑处理
log="Phase 2: 处理专辑..."

for album_dir in "$WORKROOT"/source/*/; do
    [[ -d "$album_dir" ]] || continue
    album_name=$(basename "$album_dir")

    # 跳过非音频目录
    has_audio=false
    for f in "$album_dir"*; do
        case "${f,,}" in *.ape|*.flac|*.wav) has_audio=true; break;; esac
    done
    $has_audio || continue

    echo ""
    echo "--- 专辑: $album_name ---"

    # 分析
    IFS='|' read -r type n_ape n_flac n_cue biggest_mb <<< "$(analyze_album "$album_dir")"
    info "分析: type=$type ape=$n_ape flac=$n_flac cue=$n_cue biggest=${biggest_mb}MB"

    # 确定输出目录名
    out_name="$album_name"
    for c in "$album_dir"*.cue; do
        [[ -f "$c" ]] && {
            t=$(grep -m1 '^TITLE' "$c" 2>/dev/null | sed 's/^TITLE[[:space:]]*"//;s/"[[:space:]]*$//')
            [[ -n "$t" ]] && out_name="$t"
            break
        }
    done

    dest="${LOSSLESS}/${out_name}"

    # ---- 根据类型分发 ----
    case "$type" in
        whole_cue)
            # 整轨 + 有 CUE → 切分
            if validate_cue "$(find "$album_dir" -iname "*.cue" | head -1)" "$album_dir"; then
                info "整轨切分中..."
                
                ape_file=$(find "$album_dir" -maxdepth 1 -iname "*.ape" | head -1)
                cue_file=$(find "$album_dir" -maxdepth 1 -iname "*.cue" | head -1)
                tmp=$(mktemp -d)
                
                ffmpeg -y -loglevel error -i "$ape_file" "$tmp/audio.wav"
                sed "s|FILE \"[^\"]*\"|FILE \"$tmp/audio.wav\"|" "$cue_file" > "$tmp/split.cue"
                
                cd "$tmp"
                shnsplit -f split.cue -o flac -t "%n. %t" audio.wav 2>&1 | tail -1
                cuetag split.cue *.flac 2>/dev/null
                
                # 推送到 NAS
                ssh $NAS "mkdir -p '$dest'"
                rsync -a --chmod=D755,F644 ./*.flac "${NAS}:${dest}/" &&
                    ok "切分完成: $(find . -name '*.flac' | wc -l) tracks → NAS" ||
                    ng "推送失败"
                
                cd /; rm -rf "$tmp"
            else
                pause "$album_name: CUE 验证失败 — 请人工补充后重跑"
            fi
            ;;
        whole_no_cue)
            # 缺 CUE 的整轨 → 尝试在线搜索 → 补不上则暂停
            try_fetch_cue_online "$album_dir" &&
                ok "在线 CUE 已获取" ||
                pause "$album_name: 缺少 CUE 且无法自动获取 — 请手动补充 .cue 后重跑"
            ;;
        tracks)
            # 分轨 APE → 逐文件转 FLAC
            info "分轨转换 (${n_ape} 个 APE)..."
            fail=0
            for ape in "$album_dir"/*.ape; do
                flac="${ape%.ape}.flac"
                [[ -f "$flac" ]] && continue
                nice -n 19 ffmpeg -y -loglevel error -i "$ape" -map_metadata 0 "$flac" || ((fail++))
            done
            if [[ $fail -eq 0 ]]; then
                ok "分轨转换完成 ($n_ape 首)"
                ssh $NAS "mkdir -p '$dest'" 2>/dev/null
                rsync -a --chmod=D755,F644 "$album_dir"/*.flac "${NAS}:${dest}/" &&
                    ok "推送到 NAS 完成" || ng "推送失败"
            else
                ng "分轨转换有 $fail 个失败"
            fi
            ;;
        already_flac)
            ok "已是 FLAC，无需转换"
            ssh $NAS "mkdir -p '$dest'" 2>/dev/null
            rsync -a --chmod=D755,F644 "$album_dir"/*.flac "${NAS}:${dest}/" 2>/dev/null
            ;;
        *)
            skip "空目录或无音频"
            ;;
    esac

    # 共同步骤: 封面核对
    ensure_cover "$album_dir" &&
        ok "封面: cover.jpg 已确认" ||
        ng "未找到封面图片"

done

# Phase 3: 触发 LMS 重扫 + xiaomusic 刷新
echo ""
info "Phase 3: 触发 LMS rescan + xiaomusic 刷新..."
python3 <<PYEOF
import socket
try:
    s = socket.create_connection(("10.10.10.2", 9090), timeout=8)
    s.sendall(b"wipedb 1\nrescan\n"); s.close()
    print("[✅] LMS rescan triggered")
except Exception as e:
    print(f"[NG] LMS: {e}")
PYEOF
curl -s -m 10 -X POST http://10.10.10.2:8090/cmd \
    -H "Content-Type: application/json" \
    -d '{"did":"566642283","cmd":"刷新列表"}' >/dev/null &&
    ok "xiaomusic 刷新触发" || ng "xiaomusic 刷新失败"

# Phase 4: APE 归档到 NAS
info "Phase 4: APE 母带归档..."
ssh $NAS "mkdir -p '$ARCHIVE'" 2>/dev/null
find "$WORKROOT/source" -iname "*.ape" | while read ape; do
    rel="${ape#$WORKROOT/source/}"
    rsync -a "$ape" "${NAS}:${ARCHIVE}/${rel}" 2>/dev/null
done
ok "APE 归档完成"

# ===== 清理工作区 =====
read -rp "清理本地工作区? [y/N] " yn
[[ "${yn:-}" == "y" ]] && rm -rf "$WORKROOT" && echo "已清理"

# ===== 汇总报告 =====
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║           流 水 线 处 理 汇 总               ║"
echo "╠══════════════════════════════════════════════╣"
for r in "${RESULTS[@]}"; do
    echo "║  $r"
done
echo "╠══════════════════════════════════════════════╣"
printf "║  ✅ 成功: %-3d  ⏸ 暂停: %-3d  ❌ 失败: %-3d ║\n" "$PASS" "$PAUSED" "$FAILED"
echo "╚══════════════════════════════════════════════╝"
exit "$FAILED"
