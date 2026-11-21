#!/bin/bash
# 部署前置：以代码库为唯一权威，检测三类漂移/缺失。
#
# 事故背景（2026-11-21）：把仓库 deploy/docker-compose.yml 整体 scp 到 NAS，覆盖掉了
# NAS 独有的 BRIDGE_TOKEN 环境变量 → musicctl/speak 全部在客户端前置失败，
# 表现为「设备未就绪」，用户侧只觉得点歌不放。
#
# 权威模型（用户定，2026-11-21）：
# 1. **代码库是唯一权威**：compose / config.py / 部署脚本的形态都来自仓库；
# 2. **NAS 只承载值**：`.env` 存凭据值（不入库），键名必须出现在 `.env.example`；
# 3. NAS 出现仓库没有的配置行 = **漂移**，必须先回写仓库再部署，禁止就地改 NAS。
#
# 用法：bash preflight-deploy.sh   （在 bridge/deploy 下，WSL 侧执行）
# 退出码：0 = 可部署；1 = 有漂移或必填键缺失（禁止部署）。
set -u
NAS=${NAS:-zxsadmin@10.10.10.2}
DIR=${DIR:-/volume2/docker/open-xiaoai-bridge}
HERE=$(cd "$(dirname "$0")" && pwd)
rc=0

check_file() {
  f="$1"
  tmp=$(mktemp)
  if ! ssh -o BatchMode=yes "$NAS" "cat $DIR/$f" > "$tmp" 2>/dev/null; then
    echo "WARN: 读不到 NAS $DIR/$f（跳过）"
    rm -f "$tmp"
    return 0
  fi
  d=$(mktemp)
  diff -u "$HERE/$f" "$tmp" > "$d" 2>/dev/null || true
  # diff -u repo nas：'+' 行 = NAS 独有（覆盖即丢失）；排除注释与空行
  lost=$(grep -E '^\+[^+]' "$d" | sed 's/^+//' | grep -vE '^[[:space:]]*(#|$)' || true)
  if [ -n "$lost" ]; then
    echo "🔴 $f: NAS 有仓库没有的内容（覆盖会丢失）："
    echo "$lost" | sed 's/^/    /'
    rc=1
  else
    echo "OK  $f: 无 NAS 独有内容"
  fi
  rm -f "$tmp" "$d"
}

echo "=== preflight: repo deploy/ vs NAS $DIR ==="
check_file docker-compose.yml
check_file config.py

echo "=== 必填 .env 键（值不入库；缺键 = 功能静默失败）==="
REQUIRED="OPEN_XIAOAI_TOKEN ADMIN_TOKEN BRIDGE_TOKEN SONGLOFT_PASSWORD"
envkeys=$(ssh -o BatchMode=yes "$NAS" "sed -n 's/^\\([A-Za-z_0-9]*\\)=.*/\\1/p' $DIR/.env" 2>/dev/null || true)
if [ -z "$envkeys" ]; then
  echo "🔴 读不到 NAS .env"
  rc=1
else
  for k in $REQUIRED; do
    if echo "$envkeys" | grep -qx "$k"; then
      echo "OK   .env $k"
    else
      echo "🔴 .env 缺键: $k（见 .env.example）"
      rc=1
    fi
  done
  # 业务写类鉴权：BRIDGE_API_TOKEN 与具名集合 BRIDGE_API_TOKENS 二者至少有一个（auth.py 语义）
  if echo "$envkeys" | grep -qx "BRIDGE_API_TOKEN" || echo "$envkeys" | grep -qx "BRIDGE_API_TOKENS"; then
    echo "OK   .env BRIDGE_API_TOKEN|BRIDGE_API_TOKENS"
  else
    echo "🔴 .env 缺键: BRIDGE_API_TOKEN（或 BRIDGE_API_TOKENS）"
    rc=1
  fi
fi

echo "=== 说明：.env 只同步键名说明，值永不出库 ==="
if [ "$rc" -ne 0 ]; then
  echo "❌ 存在漂移或缺失：先回写仓库 / 补 NAS .env，再部署"
fi
exit "$rc"
