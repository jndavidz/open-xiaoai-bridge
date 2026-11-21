#!/bin/bash
# 升级群晖 bridge 镜像为本地构建版本（**代码库 = 唯一权威**）。
# 三步：① 推镜像 ② 同步 config.py + docker-compose.yml（.env 的**值**永不出库）③ 重建容器。
#
# 🔴 2026-11-21 事故：旧版曾把 NAS 本地手改的 compose 当权威、仓库同步时抹掉 NAS 独有 env
# （BRIDGE_TOKEN）→ 9102 调用全失败。定规：形态以仓库为准；NAS 有仓库没有的配置行 =
# 漂移，必须先回写仓库；preflight 不通过则禁止部署。
#
# 用法：bash upgrade-image.sh   （在 deploy/ 目录执行；内部先跑 preflight-deploy.sh）
set -e
cd "$(dirname "$0")"

echo ">>> [0/3] preflight：漂移 + 必填 .env 键检查（仓库为唯一权威）"
bash ./preflight-deploy.sh

echo ">>> [1/3] 保存本地镜像并传输到群晖..."
docker save open-xiaoai-bridge:home | ssh -o BatchMode=yes zxsadmin@10.10.10.2 '/usr/local/bin/docker load'
echo ">>> [1/3] 镜像已加载"

echo ">>> [2/3] 同步 config.py + docker-compose.yml（仓库为权威；.env 只校验键名，不传值）..."
scp -O -q -o BatchMode=yes config.py docker-compose.yml zxsadmin@10.10.10.2:/volume2/docker/open-xiaoai-bridge/
echo ">>> [2/3] config.py / docker-compose.yml 已同步"

echo ">>> [3/3] 重建容器..."
ssh -o BatchMode=yes zxsadmin@10.10.10.2 'cd /volume2/docker/open-xiaoai-bridge && /usr/local/bin/docker compose up -d --force-recreate'

echo ">>> 完成。验证："
echo "  curl http://10.10.10.2:9092/api/health"
echo "  ssh zxsadmin@10.10.10.2 '/usr/local/bin/docker logs -f open-xiaoai-bridge'"
