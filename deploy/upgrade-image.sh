#!/bin/bash
# 升级群晖 bridge 镜像为本地构建版本（含 OpenAI/QwenPaw 后端）
# 用法：bash upgrade-image.sh  （在 deploy/ 目录执行）
set -e
cd "$(dirname "$0")"

echo ">>> [1/3] 保存本地镜像并传输到群晖..."
docker save open-xiaoai-bridge:home | ssh -o BatchMode=yes zxsadmin@10.10.10.2 '/usr/local/bin/docker load'
echo ">>> [1/3] 镜像已加载"

echo ">>> [2/3] 同步最新 deploy 配置..."
tar cf - config.py docker-compose.yml .env | ssh -o BatchMode=yes zxsadmin@10.10.10.2 'tar xf - -C /volume2/docker/open-xiaoai-bridge/'

echo ">>> [3/3] 重建容器..."
ssh -o BatchMode=yes zxsadmin@10.10.10.2 'cd /volume2/docker/open-xiaoai-bridge && sed -i "s|image: ghcr.nju.edu.cn/coderzc/open-xiaoai-bridge:latest|image: open-xiaoai-bridge:home|" docker-compose.yml && /usr/local/bin/docker compose up -d --force-recreate'

echo ">>> 完成。验证："
echo "  curl http://10.10.10.2:9092/api/health"
echo "  ssh zxsadmin@10.10.10.2 '/usr/local/bin/docker logs -f open-xiaoai-bridge'"
