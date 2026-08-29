# REBUILD.md —— bridge 容器构建与验收手册

> 适用：群晖 NAS 部署 `open-xiaoai-bridge`（fork 深度本地演进版）。
> 配套：`docker-compose.yml`、`config.py`（生产版）、`.env`（不入库）、`models/`。
> 构建有两条等价路径：**A. WSL 桌面机构建→推镜像（推荐，快）** / **B. NAS 本地编译（备选）**。
> 任一路径选好后，**部署与验收（§2/§3）完全相同**；两者铁律一致：改 `core/` 或增删唤醒词必须重建镜像。

## 0. 为什么必须本地构建（不是拉上游镜像）

`docker-compose.yml` 此前引用上游 `ghcr.nju.edu.cn/coderzc/open-xiaoai-bridge:latest`，
但本仓库是 fork 且深度演进，**以下改动烤进镜像、bind-mount 覆盖不到**：

- `core/services/audio/kws/__init__.py`（T7.6 `listen_disabled` 持久开关等）
- `core/services/api_server.py`（T7.6 `POST /api/audio_input` 恢复通道等）
- 任何 `core/` 下的代码改动

只有 `config.py` 是 bind-mount（`./config.py:/app/config.py`），改它能热重载参数值，
**但代码改动不会进容器**。因此 compose 已改为 `build: { context: .. }` + `image: open-xiaoai-bridge:local`，
从 fork 源码（`bridge/` 根）本地构建。

> ⚠️ 直接 `docker compose restart` 上游镜像 = 功能 broken：喊「停止聆听」时
> `get_kws().disable_listening()` 在镜像里不存在 → `AttributeError`。**务必重建镜像并 recreate。**

## 1. 构建（两条路径，二选一）

### 路径 A：WSL 桌面机构建 → 推送 NAS（推荐，2026-08-26 实战验证）

桌面机（i3-12100T）编译比 NAS 的 N3060 快得多，镜像经 SSH 流式传输：

```bash
# 在 bridge/ 仓库根（构建上下文 = 仓库根，含 core/ native/）
cd /mnt/d/repos/open-xiaoai/bridge

# 沙箱/受限环境：docker CLI 需写 ~/.docker，用 DOCKER_CONFIG 重定向到可写位置
export DOCKER_CONFIG=/tmp/docker-config

docker build -t open-xiaoai-bridge:home .
# 产物约 431MB；成功标志 = 末尾 "#n naming to docker.io/library/open-xiaoai-bridge:home done"
```

**WSL 路径踩坑实录**：
- `mkdir ~/.docker: permission denied` → 加 `DOCKER_CONFIG=/tmp/docker-config`（见上）；
- `sh.rustup.rs` 偶发 DNS 解析失败（`curl exit 6`）→ **重试即过**，非持久故障；
- `COPY --from=ghcr.io/astral-sh/uv:0.7` 直连超时（ghcr 被墙）→ Dockerfile 已改为
  **`ghcr.nju.edu.cn/astral-sh/uv:0.7`**（提交 `e3d7df3`），无需再动。

推送到群晖（SSH 流式，免 scp 中转）：

```bash
cd bridge/deploy && DOCKER_CONFIG=/tmp/docker-config \
  docker save open-xiaoai-bridge:home | \
  ssh -o BatchMode=yes zxsadmin@10.10.10.2 '/usr/local/bin/docker load'
# NAS 侧若已有同名镜像自动改名保留（可回滚）
```

### 路径 B：NAS 本地编译（备选）

源码已 rsync 到 `/volume2/docker/open-xiaoai-bridge-src`（构建上下文），直接显式构建：

```bash
/usr/local/bin/docker build -t open-xiaoai-bridge:home /volume2/docker/open-xiaoai-bridge-src
```

> PC 侧 `bridge/deploy/docker-compose.yml` 另含 `build: { context: .. }` + `image: open-xiaoai-bridge:local` 变体，
> 适用于把 `bridge/` 当上下文的工作站；**群晖部署统一用 `open-xiaoai-bridge:home` 这个 tag**（A/B 两路径产物同名）。

- 构建上下文 `.dockerignore` 已排除 `.git`/`.venv`/`target`/`deploy`/`models`，上下文干净。
- 镜像内 `keywords.txt` 由 `Dockerfile` CMD 在**容器启动时**经 `keywords.py` 编译，
  来源是 bind-mount 进来的 `config.py` 的 `wakeup.keywords`（含「停止聆听」）。

## 2. 部署 / 重建容器

```bash
# 路径 A 之后（镜像已在 NAS）：直接强制重建
ssh -o BatchMode=yes zxsadmin@10.10.10.2 \
  'cd /volume2/docker/open-xiaoai-bridge && /usr/local/bin/docker compose up -d --force-recreate'

# 路径 B 之后（在 NAS 本地构建完）：同上一条即可；或在 PC 侧 compose 变体下：
cd bridge/deploy
docker compose up -d --build --force-recreate
```

容器启动即重跑 CMD → 重新编译 `keywords.txt`（含新增唤醒词）+ 以 fork 代码运行 `main.py`。

> ⚠️ **部署遗漏重灾**（2026-08-26 实踩）：只推镜像（路径 A）而**不同步 config.py** → 新唤醒词全部失效
> （「你好贾维斯」可唤醒、「你好老师」无反应——因为词表在 **bind-mount 的 config.py**，不随镜像走）。
> 完整部署 = **① 推镜像 + ② 同步 `deploy/config.py`（及 compose/.env，若变更）+ ③ recreate** 三步缺一不可；
> 这是 `upgrade-image.sh` 的三步流程，勿跳。仅改 config.py 时：推文件 + `docker compose restart` 即可热更。

## 3. 验收清单（部署后逐条过）

| # | 项目 | 操作 | 期望 |
|---|------|------|------|
| 1 | 容器健康 | `docker compose ps` / 日志 `docker compose logs -f` | 无启动报错；见 `关键词文件生成完成` |
| 2 | T7.3 WOL | 说「打开电脑」 | NUC 唤醒（HA `script.wake_nuc` 触发） |
| 3 | T8 工具闭环 | 说「北京天气怎么样」 | DeepSeek 调度 Open-Meteo 工具，返回实况天气 |
| 4 | T7.6 关 | 喊「停止聆听」 | 麦克风停 + TTS「已停止聆听」；日志见 `set_mic(False)` / `disable_listening()` |
| 5 | T7.6 开（唯一恢复路径） | `curl -X POST http://<nas>:9092/api/audio_input` | `{"success":true,"mic":"on","listening":true}` |
| 6 | T7.6 静默回退 | 停止聆听后再喊任意词 | 无响应（语音通道已关，符合设计） |

## 4. 改动类型 → 生效方式速查

| 改动内容 | 生效方式 |
|----------|----------|
| 仅 `APP_CONFIG` 参数值（vad 阈值等） | config.py bind-mount → **热重载 ~1s**，无需重建 |
| 新增/删除 `wakeup.keywords` 唤醒词 | 必须**重建+recreate**（词表仅启动时编译） |
| 任何 `core/` 代码改动 | 必须**重建+recreate**（代码烤进镜像） |

## 5. 已知待回写结论（实机验收后补 runbook）

- `speaker.set_mic(False)` 是否同时停掉 GlobalStream 本地 PCM 采集；
  若只停云 ASR、本地 KWS 仍吃 PCM，则 `listen_disabled` 持久暂停已兜底，无需额外改动。
- T7.5 截胡边界周级调优（`XIAOAI_COMMANDS` 当前仅「打开电脑」最小集）。
