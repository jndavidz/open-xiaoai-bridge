# REBUILD.md —— bridge 容器构建与验收手册

> 适用：群晖 NAS 部署 `open-xiaoai-bridge`（fork 深度本地演进版）。
> 配套：`docker-compose.yml`、`config.py`（生产版）、`.env`（不入库）、`models/`。
> 构建有两条等价路径：**A. WSL 桌面机构建→推镜像（推荐，快）** / **B. NAS 本地编译（备选）**。
> 任一路径选好后，**部署与验收（§2/§3）完全相同**；两者铁律一致：改 `core/` 或增删唤醒词必须重建镜像。
>
> ✅ **可复现构建（ticket 14，2026-10-24）**：见 §1.5。清缓存干净构建实测 **7m49s**（此前 cargo 层 9.5min 超时已消除：
> Rust 改由 maturin 后端单次编译 + BuildKit cache mount），两次构建 RootFS 层 diffID 逐层一致。

## 0. 为什么必须本地构建（不是拉上游镜像）

`docker-compose.yml` 此前引用上游 `ghcr.nju.edu.cn/coderzc/open-xiaoai-bridge:latest`，
但本仓库是 fork 且深度演进，**以下改动烤进镜像、bind-mount 覆盖不到**：

- `core/services/audio/kws/__init__.py`（T7.6 `listen_disabled` 持久开关等）
- `core/services/api_server.py`（T7.6 `POST /api/audio_input` 恢复通道等）
- 任何 `core/` 下的代码改动

只有 `config.py` 是 bind-mount（`./config.py:/app/config.py`），改它能热重载参数值，
**但代码改动不会进容器**。因此部署统一用本地构建产物 `image: open-xiaoai-bridge:home`（compose **不含 build 段**，镜像由显式 `docker build -t open-xiaoai-bridge:home` 提供，见 §1），
从 fork 源码（`bridge/` 根）本地构建。

> ⚠️ 直接 `docker compose restart` 上游镜像 = 功能 broken：喊「停止聆听」时
> `get_kws().disable_listening()` 在镜像里不存在 → `AttributeError`。**务必重建镜像并 recreate。**

## 1. 构建（两条路径，二选一）

### 路径 A：WSL 桌面机构建 → 推送 NAS（推荐，2026-08-26 实战验证）

桌面机（i3-12100T）编译比 NAS 的 N3060 快得多，镜像经 SSH 流式传输：

```bash
# 在 bridge/ 仓库根（构建上下文 = 仓库根，含 core/ native/）
cd /mnt/d/_work/repos/open-xiaoai/bridge

# 沙箱/受限环境：docker CLI 需写 ~/.docker，用 DOCKER_CONFIG 重定向到可写位置
export DOCKER_CONFIG=/tmp/docker-config

docker build -t open-xiaoai-bridge:home .
# 产物约 400–600MB（随依赖演进）；成功标志 = 末尾 "#n naming to docker.io/library/open-xiaoai-bridge:home done"
```

**WSL 路径踩坑实录**：
- `mkdir ~/.docker: permission denied` → 加 `DOCKER_CONFIG=/tmp/docker-config`（见上）；
- `sh.rustup.rs` 偶发 DNS 解析失败（`curl exit 6`）→ **重试即过**，非持久故障；
- `COPY --from=ghcr.io/astral-sh/uv:0.7` 直连超时（ghcr 被墙）→ Dockerfile 已改为
  **`ghcr.nju.edu.cn/astral-sh/uv:0.7`**（提交 `e3d7df3`），无需再动。

推送到群晖（SSH 流式，免 scp 中转）：

```bash
DOCKER_CONFIG=/tmp/docker-config \
  docker save open-xiaoai-bridge:home | \
  ssh -o BatchMode=yes zxsadmin@10.10.10.2 '/usr/local/bin/docker load'
# NAS 侧若已有同名镜像自动改名保留（可回滚）
```

### 路径 B：NAS 本地编译（备选）

源码已 rsync 到 `/volume2/docker/open-xiaoai-bridge-src`（构建上下文），直接显式构建：

```bash
/usr/local/bin/docker build -t open-xiaoai-bridge:home /volume2/docker/open-xiaoai-bridge-src
```

> PC 侧 `bridge/deploy/docker-compose.yml` 与群晖部署目录的 compose **同源**——均为 `image: open-xiaoai-bridge:home`（无 build 段）；
> 构建由显式 `docker build -t open-xiaoai-bridge:home` 完成（路径 A 在 WSL、路径 B 在 NAS），不走 compose build。**群晖部署统一用 `:home` 这个 tag**（A/B 两路径产物同名）。

- 构建上下文 `.dockerignore` 已排除 `.git`/`.venv`/`target`/`deploy`/`models`，上下文干净。
  ⚠️ **`Cargo.lock` 已不再被排除（ticket 14）**：它随 `native/` 进构建上下文，锁住 git 依赖 commit
  与全部 crates 版本——升级依赖时手工 `uv lock`（Python 侧）/容器内 `cargo upgrade`（Rust 侧）后提交。
- 镜像内 `keywords.txt` 由 `Dockerfile` CMD 在**容器启动时**经 `keywords.py` 编译，
  来源是 bind-mount 进来的 `config.py` 的 `wakeup.keywords`（含「停止聆听」）。

### 1.5 可复现构建口径（ticket 14，2026-10-24）

| 项 | 做法 |
|---|---|
| 基础镜像 | `python:3.12-slim@sha256:a0f57064…`（manifest digest 钉死，两 stage 同）；升级=手工换 digest |
| uv 工具镜像 | `ghcr.nju.edu.cn/astral-sh/uv:0.7@sha256:62924083…` |
| 平台 | 两 stage 显式 `--platform=linux/amd64`（BuildKit 会 WARN 常量 platform，属预期） |
| Rust | `rustup -y --default-toolchain 1.99.1 --profile minimal`（`RUST_TOOLCHAIN` env，升级手工改） |
| Rust 依赖 | `native/Cargo.lock` 入库（210 项，git 依赖 `open-xiaoai` 钉 `14e1034b`） |
| Python 依赖 | `uv.lock` + `uv sync --locked` |
| 缓存 | BuildKit cache mount：`/root/.cargo/{registry,git}`、`/root/.cache/uv`、`/app/native/target`（不进层、跨构建复用） |
| 扩展安装 | maturin **构建后端**经 `uv sync --locked --no-editable` 单次编译并装入 `.venv`；已废除 `uv run maturin build` + `uv remove maturin`（后者层内改写锁文件且重复编译——9.5min 超时的根因） |

实测（WSL，2026-10-24）：全清缓存（`docker builder prune -af`）干净构建 **469s**；紧接第二次构建 2s（层全命中），
两次 RootFS 层 diffID **逐层一致**；`import open_xiaoai_server` 验证通过。

⚠️ **在宿主机跑 `uv lock` 必须带与镜像一致的源**，否则 lock 会被整体改写成另一 registry 的 URL：
`UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple uv lock`（WSL 默认 uv.toml 是 aliyun，勿裸跑）。

## 2. 部署 / 重建容器

```bash
# 路径 A 之后（镜像已在 NAS）：直接强制重建
ssh -o BatchMode=yes zxsadmin@10.10.10.2 \
  'cd /volume2/docker/open-xiaoai-bridge && /usr/local/bin/docker compose up -d --force-recreate'

# 路径 B 之后（在 NAS 本地构建完）：同上一条即可；或在 WSL 工作站本地起：
cd bridge && docker build -t open-xiaoai-bridge:home .
cd bridge/deploy && docker compose up -d --force-recreate
```

容器启动即重跑 CMD → 重新编译 `keywords.txt`（含新增唤醒词）+ 以 fork 代码运行 `main.py`。

> ⚠️ **部署遗漏重灾**（2026-08-26 实踩）：只推镜像（路径 A）而**不同步 config.py** → 新唤醒词全部失效
> （「你好小塔」可唤醒、「你好助教」无反应——因为词表在 **bind-mount 的 config.py**，不随镜像走）。
> 完整部署 = **① 推镜像 + ② 同步 `deploy/config.py`（及 compose/.env，若变更）+ ③ recreate** 三步缺一不可；
> 这是 `upgrade-image.sh` 的三步流程，勿跳。仅改 config.py 时：推文件 + `docker compose restart` 即可热更。

## 3. 验收清单（部署后逐条过）

| # | 项目 | 操作 | 期望 |
|---|------|------|------|
| 1 | 容器健康 | `docker compose ps` / 日志 `docker compose logs -f` | 无启动报错；见 `关键词文件生成完成` |
| 2 | T7.3 WOL | **须说"小爱同学，打开电脑"**（xiaoai 截胡）；直接说"打开电脑"无效 | NUC 唤醒（HA `switch.nuc_hifi_wol` turn_on）→ 实测 `HA switch.turn_on OK` ✓ |
| 3 | T8 工具闭环 | **先喊"你好小塔"进对话再问**；连说"小塔，北京天气怎么样"无效（唤醒词是"你好小塔"） | OpenAI 兼容后端调度 Open-Meteo → 实测 `tool round: ['weather_get']` ✓ |
| 4 | T7.6 关 | 喊「停止聆听」 | 麦克风停 + TTS「已停止聆听」；日志见 `set_mic(False)` / `disable_listening()` |
| 5 | T7.6 开（唯一恢复路径） | `curl -X POST http://<nas>:9092/api/audio_input` | `{"success":true,"mic":"on","listening":true}` |
| 6 | T7.6 静默回退 | 停止聆听后再喊任意词 | 无响应（语音通道已关，符合设计） |

## 4. 改动类型 → 生效方式速查

| 改动内容 | 生效方式 |
|----------|----------|
| 仅 `APP_CONFIG` 参数值（vad 阈值等） | config.py bind-mount → **热重载 ~1s**，无需重建 |
| 新增/删除 `wakeup.keywords` 唤醒词 | 必须**重建+recreate**（词表仅启动时编译） |
| 任何 `core/` 代码改动 | 必须**重建+recreate**（代码烤进镜像） |

## 5. 实机验收结论（2026-08-29 回填）

### 5.1 set_mic / 静音机制（T7.6）
- `set_mic(False)` 经 `run_shell` 在**设备宿主侧**建 `/tmp/mipns/mute` → 设备真静音（隐私关闭成立）。
- **关键坑（已修复）**：`set_mic(True)` 只发 ubus event:7，**不删 mute 文件** → 设备仍静音 → ④⑤ 收不到语音零事件。
  修复：`handle_audio_input` 在 `set_mic(True)` 后显式 `rm -f /tmp/mipns/mute`（与关对称）+ 日志 `[APIServer] /api/audio_input 恢复: mic unmuted`；修复已入镜像 `b8cc1961`，恢复通道现返回 `mic:"on"`。
- 本地 KWS 仍吃 PCM（二次"停止聆听"能触发=KWS 离线识别不依赖云语音流）；`listen_disabled` 持久暂停兜底成立，**无需**改动 GlobalStream。

### 5.2 截胡边界与唤醒词（T7.3 / T8）
- 「打开电脑」=`XIAOAI_COMMANDS`（xiaoai 分支）→ HA `switch.nuc_hifi_wol` WOL。
  **须"小爱同学，打开电脑"**（小爱唤醒才把指令转发 bridge 截胡）；直接说"打开电脑"无小爱前缀→不进 bridge→不唤醒。
- AI 对话唤醒词 = **「你好小塔」**（2026-10-27 起定稿；session_key 沿用 agent:javis）/ **「你好助教」**（辅导，agent:tutor）。旧词「你好贾维斯 / 你好老师」已退役。
  **连说"小塔，北京天气怎么样"无效**——KWS 词表仅「你好小塔」整词，无单独「小塔」。须分两步：喊「你好小塔」听到"我在"→再说问题。
- 文档已修正：`README` 第75行错误样例"贾维斯，现在上海天气怎么样"（连说无效）已改为「你好贾维斯」进对话再问；「贾维斯」短唤醒词**保持现状不加**（用户 08-29 决定）。

### 5.3 本轮验收结果（全绿）
- ① 停止聆听关+TTS ✓　② 静默回退 ✓　③ 恢复 mic:on ✓
- ④ NUC 唤醒 ✓（04:34 `HA switch.turn_on OK`）　⑤ 天气工具闭环 ✓（多次 `tool round: ['weather_get']`）
