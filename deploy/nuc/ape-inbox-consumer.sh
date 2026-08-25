#!/bin/bash
# ============================================================
# ape-inbox-consumer.sh — APE 入库消费者
#
# 功能：扫描 NAS inbox/ 下的专辑子目录，逐个处理：
#   APE→FLAC / CUE切分 / 封面核对 / booklet瘦身 / 归档 / 清理
#
# 用法（在 NUC root 下执行）：
#   bash ape-inbox-consumer.sh              # 处理全部待处理目录
#   bash ape-inbox-consumer.sh <子目录名>   # 只处理指定目录
#
# 你的操作流（每张 DVD）：
#   1. 家庭PC光驱读盘 → 复制整张盘内容到 NAS:
#      /volume1/music/inbox/disc-<编号或名称>/
#   2. 告诉我"盘到了"
#   3. 我跑本脚本 → 完成后通知你换盘
# ============================================================
set -uo pipefail

NAS="nas"                              # ~/.ssh/config 别名 → zxsadmin@10.10.10.2
INBOX_REMOTE="/volume1/music/inbox"
LOSSLESS="/volume1/music/lossless/classical"
BOOKLETS="/volume1/music/booklets"
ARCHIVE="/volume1/music/archive_ape"
WORKROOT="/var/tmp/music-pipeline"
PARALLEL=2

PASS=0; PAUSED=0; FAILED=0
declare -a RESULTS

ok()   { RESULTS+=("✅ $*"); PASS=$((PASS+1)); echo "  [✅] $*"; }
pause(){ RESULTS+=("⏸ $*"); PAUSED=$((PAUSED+1)); echo "  [⏸] $*"; }
ng()   { RESULTS+=("❌ $*"); FAILED=$((FAILED+1)); echo "  [❌] $*"; }

echo "═══════════════════════════════════════"
echo " APE 入库消费者 $(date '+%F %T')"
echo "═══════════════════════════════════════"

# ===== 获取 inbox 目录列表 =====
INBOX_LIST=$(ssh $NAS "ls -d ${INBOX_REMOTE}/*/ 2>/dev/null" | sed 's|.*/\(.*\)/|\1|')

if [[ -z "$INBOX_LIST" ]]; then
    echo "[inbox 为空 — 没有待处理目录]"
    exit 0
fi

# 过滤：只处理指定目录或全部
if [[ $# -ge 1 ]]; then
    INBOX_LIST=$(echo "$INBOX_LIST" | grep "^$1$")
fi

TOTAL_DIRS=$(echo "$INBOX_LIST" | wc -l)
echo "待处理目录: $TOTAL_DIRS 个"
echo ""

# ===== 逐目录处理 =====
for disc_name in $INBOX_LIST; do
    remote_dir="${INBOX_REMOTE}/${disc_name}"
    local_dir="${WORKROOT}/${disc_name}"

    echo ""
    echo "━━━ 处理: $disc_name ━━━"

    # ---- [1] rsync 拉取到本地 ----
    mkdir -p "$local_dir"
    rsync -a --info=progress2 "${NAS}:${remote_dir}/" "${local_dir}/" 2>&1 | tail -1
    
    file_count=$(find "$local_dir" -type f | wc -l)
    [[ $file_count -eq 0 ]] && { ng "$disc_name: rsync 后为空"; continue; }
    ok "拉取完成: $file_count 个文件"

    # ---- [2] 分析: 整轨/分轨/CUE ----
    n_ape=0; n_flac=0; n_cue=0; biggest=0
    for f in "$local_dir"/*; do
        [[ -f "$f" ]] || continue
        case "${f,,}" in
            *.ape)
                ((n_ape++))
                sz=$(stat -c%s "$f"); ((sz > biggest)) && biggest=$sz ;;
            *.flac) ((n_flac++)) ;;
            *.cue)  ((n_cue++)) ;;
        esac
    done
    biggest_mb=$((biggest / 1048576))

    # 分类判定
    if [[ $n_ape -eq 1 && $biggest_mb -ge 30 ]]; then
        if [[ $n_cue -ge 1 ]]; then
            album_type="whole_cue"
        else
            # 尝试从同目录 log/txt 文件推断曲目信息，生成简易 CUE
            info "整轨无 CUE — 尝试从 EAC/XLD 日志提取..."
            log_file=$(find "$local_dir" -iname "*.log" | head -1)
            if [[ -n "$log_file" ]] && grep -q "TRACK\|Track" "$log_file"; then
                pause "$disc_name: 有抓轨日志但需人工核对生成 CUE — 已归入 archive_ape 待处理"
                # 归档 APE 等待人工处理
                ssh $NAS "mkdir -p '$ARCHIVE/$disc_name'" 2>/dev/null
                rsync -a --remove-source-files "$local_dir/"*ape* "${NAS}:${ARCHIVE}/$disc_name/" 2>/dev/null
                ssh $NAS "rm -rf '$remote_dir'" 2>/dev/null
                rm -rf "$local_dir"
                continue
            fi
            album_type="whole_no_cue"
        fi
    elif [[ $n_ape -gt 1 ]]; then
        album_type="tracks"
    elif [[ $n_flac -gt 0 ]]; then
        album_type="already_flac"
    else
        ng "$disc_name: 无音频文件"; rm -rf "$local_dir"; continue
    fi

    info "类型: $album_type (APE:$n_ape FLAC:$n_flac CUE:$n_cue 最大:${biggest_mb}MB)"

    # 确定输出目录名（优先 cue TITLE）
    out_name="$disc_name"
    for c in "$local_dir"/*.cue; do
        [[ -f "$c" ]] && {
            t=$(grep -m1 '^TITLE' "$c" 2>/dev/null | sed 's/^TITLE[[:space:]]*"//;s/"[[:space:]]*$//')
            [[ -n "$t" ]] && out_name="$t"
            break
        }
    done
    dest="${LOSSLESS}/${out_name}"

    # ---- 根据类型执行转换 ----
    case "$album_type" in
        whole_cue | whole_no_cue)
            cue_file=$(find "$local_dir" -iname "*.cue" | head -1)
            ape_file=$(find "$local_dir" -maxdepth 1 -iname "*.ape" | head -1)

            if [[ "$album_type" == "whole_no_cue" ]]; then
                # 无 CUE 的整轨 → 整轨直转 FLAC（不切分），标记后续可补
                info "无 CUE — 整轨直转（不切分）"
                flac_out="${local_dir}/$(basename "${ape_file%.ape}").flac"
                ffmpeg -y -loglevel error -i "$ape_file" "$flac_out" || { ng "解码失败"; continue; }
                
                ssh $NAS "mkdir -p '$dest'" 2>/dev/null
                rsync -a --chmod=D755,F644 "$flac_out" "${NAS}:${dest}/" &&
                    ok "整轨直转完成（无CUE未切分）— $(basename "$flac_out")"
                
                # APE 归档
                ssh $NAS "mkdir -p '${ARCHIVE}/${disc_name}'" 2>/dev/null
                rsync -a --remove-source-files "$ape_file" "${NAS}:${ARCHIVE}/${disc_name}/" 2>/dev/null
                rm -rf "$local_dir"
                continue
            fi

            # 有 CUE → 切分流程
            if ! validate_cue "$cue_file" "$local_dir"; then
                pause "$disc_name: CUE 验证失败 — 请人工检查后重跑"
                continue
            fi

            info "整轨切分中..."
            
            tmpdir=$(mktemp -d)
            ffmpeg -y -loglevel error -i "$ape_file" "$tmpdir/audio.wav"
            sed "s|FILE \"[^\"]*\"|FILE \"$tmpdir/audio.wav\"|" "$cue_file" > "$tmpdir/split.cue"
            
            cd "$tmpdir"
            shnsplit -f split.cue -o flac -t "%n. %t" audio.wav 2>&1 | tail -1
            cuetag split.cue *.flac 2>/dev/null
            
            track_count=$(find . -maxdepth 1 -name "*.flac" | wc -l)
            
            # 推送到 NAS
            ssh $NAS "mkdir -p '$dest'"
            rsync -a --chmod=D755,F644 ./*.flac "${NAS}:${dest}/" &&
                ok "切分完成: $track_count tracks → NAS" ||
                ng "推送失败"
            
            cd /; rm -rf "$tmpdir"
            ;;

        tracks)
            info "分轨转换 ($n_ape 首)..."
            fail=0
            for ape in "$local_dir"/*.ape; do
                flac="${ape%.ape}.flac"
                [[ -f "$flac" ]] && continue
                nice -n 19 ffmpeg -y -loglevel error -i "$ape" -map_metadata 0 "$flac" || ((fail++))
            done
            
            if [[ $fail -gt 0 ]]; then
                ng "有 $fail 个转换失败"
            else
                ok "分轨转换完成"
            fi

            ssh $NAS "mkdir -p '$dest'" 2>/dev/null
            rsync -a --chmod=D755,F644 "$local_dir"/*.flac "${NAS}:${dest}/" &&
                ok "推送到 NAS 完成" || ng "推送失败"
            ;;

        already_flac)
            ok "已是 FLAC，直接推送"
            ssh $NAS "mkdir -p '$dest'" 2>/dev/null
            rsync -a --chmod=D755,F644 "$local_dir"/*.flac "${NAS}:${dest}/"
            ;;
    esac

    # ---- 共同步骤: 封面核对补齐 ----
    cover_found=false
    for name in cover.jpg folder.jpg front.jpg Cover.jpg Cover\ Front.jpg albumart.jpg; do
        [[ -f "$local_dir/$name" ]] && cover_found=true && break
    done
    if ! $cover_found; then
        best="" sz=0
        for j in "$local_dir"/*.jpg "$local_dir"/*.jpeg; do
            [[ -f "$j" ]] || continue
            s=$(stat -c%s "$j"); ((s > sz)) && { best="$j"; sz=$s; }
        done
        [[ -n "$best" ]] && cp "$best" "$local_dir/cover.jpg" && cover_found=true
    fi
    if $cover_found; then
        ssh $NAS "mkdir -p '$dest'" 2>/dev/null
        scp -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            "$local_dir/cover.jpg" "${NAS}:${dest}/cover.jpg" 2>/dev/null &&
            ok "封面已推送" || ng "封面推送失败"
    else
        info "无封面图片"
    fi

    # ---- Booklet PDF 瘦身 ----
    for pdf in "$local_dir"/*.pdf; do
        [[ -f "$pdf" ]] || continue
        pdf_base=$(basename "$pdf" .pdf)
        slimmed="/tmp/${pdf_base}_slim.pdf"
        
        gs -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook -o "$slimmed" "$pdf" 2>/dev/null
        
        if [[ -f "$slimmed" && -s "$slimmed" ]]; then
            orig_kb=$(stat -c%s "$pdf" / 1024)
            slim_kb=$(stat -c%s "$slimmed" / 1024)
            
            ssh $NAS "mkdir -p '$BOOKLETS'" 2>/dev/null
            scp -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
                "$slimmed" "${NAS}:${BOOKLETS}/${out_name}.pdf" 2>/dev/null &&
                ok "booklet 瘦身: ${orig_kb}KB → ${slim_kb}KB → NAS" ||
                ng "booklet 推送失败"
        else
            ng "gs 瘦身失败: $pdf"
        fi
        rm -f "$slimmed"
    done

    # ---- 清理 inbox 该目录 + 本地工作区 ----
    ssh $NAS "rm -rf '$remote_dir'" 2>/dev/null && info "inbox 已清理: $disc_name"
    rm -rf "$local_dir"

done

# ===== 触发 LMS 重扫 + xiaomusic 刷新 =====
echo ""
echo "=== 触发 LMS rescan + xiaomusic 刷新 ==="
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

# ===== 汇总报告 =====
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║         A P E   入 库 汇 总                  ║"
echo "╠══════════════════════════════════════════════╣"
for r in "${RESULTS[@]}"; do
    echo "║  $r"
done
echo "╠══════════════════════════════════════════════╣"
printf "║  ✅ 成功: %-3d  ⏸ 暂停: %-3d  ❌ 失败: %-3d ║\n" "$PASS" "$PAUSED" "$FAILED"
echo "╚══════════════════════════════════════════════╝"
exit "$FAILED"
