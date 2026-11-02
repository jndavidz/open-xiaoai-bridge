#!/bin/sh
# monitor-fa-cpu.sh — FA/CPU 监控窗口数据采集（music-far 票 02 部署后观察窗口）
# 用法：NAS 上 `sh monitor-fa-cpu.sh`（办公室机经 WSL ssh 调用，见 deploy-vad-endpoint 纪要）
# 口径：shadow_only_hits = 当日影子 JSONL 中 hits 非空的记录数（含家人真声被主路
#       漏检的影子命中——票 08 前的代理指标，非纯误触发）；「停止聆听」单独点名，
#       任何非 0 即高危（误触会静默关闭整套唤醒）。
D=/volume2/docker/open-xiaoai-bridge/data/shadow
TODAY=$(date +%Y%m%d)
F=$D/kws-shadow-$TODAY.jsonl
echo "== FA $(date '+%F %T') =="
if [ -f "$F" ]; then
  TOTAL=$(wc -l < $F)
  EMPTY=$(grep -c '"hits": \[\]' $F)
  HITS=$((TOTAL - EMPTY))
  echo "records=$TOTAL shadow_only_hits=$HITS"
  echo "-- 命中词分布（top8）--"
  grep -v '"hits": \[\]' $F | grep -o '"hits": \[[^]]*\]' | sort | uniq -c | sort -rn | head -8
  STOP=$(grep -v '"hits": \[\]' $F | grep -c '停止聆听')
  echo "stop_listening_hits=$STOP (must be 0)"
else
  echo "no-jsonl-today records=0 shadow_only_hits=0 stop_listening_hits=0"
fi
echo "== CPU =="
/usr/local/bin/docker stats --no-stream --format "{{.CPUPerc}} {{.MemUsage}}" open-xiaoai-bridge
echo "== container =="
/usr/local/bin/docker ps --filter name=open-xiaoai-bridge --format "{{.Status}}"
cd /volume2/docker/open-xiaoai-bridge
ERRN=$(/usr/local/bin/docker compose logs --since 12h 2>&1 | grep -ci " error ")
echo "errors_12h=$ERRN"
