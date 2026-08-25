#!/bin/bash
# ape2flac-batch.sh — 分轨 APE 批量转 FLAC（保持目录结构, 标签自动迁移）
# 用法: bash ape2flac-batch.sh <含ape的工作目录> [并行数,默认3]
set -uo pipefail
SRC="${1:?用法: $0 <工作目录>}"
PARALLEL="${2:-3}"
PASS=0; FAIL=0

TOTAL=$(find "$SRC" -iname "*.ape" | wc -l)
echo "== 待转换 APE: $TOTAL =="

convert_one() {
    local ape="$1"
    local flac="${ape%.ape}.flac"
    [[ -f "$flac" ]] && { echo "[skip] $(basename "$flac")"; return 0; }
    if nice -n 19 ffmpeg -y -loglevel error -i "$ape" "$flac" 2>>"$SRC/../convert-errors.log"; then
        echo "[ok] $(basename "$flac")"
    else
        echo "[FAIL] $ape"; rm -f "$flac"; return 1
    fi
}
export -f convert_one

find "$SRC" -iname "*.ape" -print0 | xargs -0 -P "$PARALLEL" -I{} bash -c 'convert_one "$@"' _ {}

DONE=$(find "$SRC" -iname "*.flac" | wc -l)
ERRF="$SRC/../convert-errors.log"
ERRN=$([[ -f "$ERRF" ]] && grep -c FAIL "$ERRF" 2>/dev/null || echo 0)
echo "== 完成: FLAC $DONE 个 | 失败 $ERRN =="
[[ "$ERRN" == "0" ]]
