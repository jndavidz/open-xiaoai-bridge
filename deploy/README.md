# 家庭部署手册（deploy/）

> 目标：群晖 DS416play 上运行 open-xiaoai-bridge，小爱 Pro(LX06) 经 client 连接，
> 实现：免唤醒/截胡双指令表场景控制 + 「你好小塔」OpenAI 兼容大模型连续对话（当前 DeepSeek，可经 admin 面板热切换）+ 「你好助教」
> 学科辅导（独立人设）+ Agent 工具层（weather/hass/music 只读工具）+ 「停止聆听」隐私开关
> + HA↔音箱双向 TTS + 后台管理面板。
> 对应总方案：`../../docs/plan/home-theater-automation-plan.md` Phase C/D；构建与镜像边界见 [REBUILD.md](REBUILD.md)。

## 1. 前置

- 群晖 SSH：`ssh zxsadmin@10.10.10.2`，docker 全路径 `/usr/local/bin/docker`
- LX06 已刷补丁固件、client-rust 可运行（SSH：`ssh -o HostKeyAlgorithms=+ssh-rsa root@10.10.10.20`，密码 open-xiaoai）
- 大模型 API Key（当前 DeepSeek，OpenAI 兼容；可经 admin 面板热切换）、HA 长期访问令牌

## 2. 本地准备（WSL2）

```bash
cd /mnt/d/_work/repos/open-xiaoai/bridge/deploy
cp .env.example .env && vim .env      # 必填 4（OPEN_XIAOAI_TOKEN / DEEPSEEK_API_KEY / HA_TOKEN / ADMIN_TOKEN）+ 业务鉴权 BRIDGE_API_TOKENS（具名 label:token 集合，旧单值 BRIDGE_API_TOKEN 已于 2026-10-24 迁移收口）+ 可选（INJECT_* / HA_BASE_URL / MONITOR_SERVICES / TURN_TRACE_ENABLED），逐项说明见 .env.example
# 下载模型包（VAD+KWS；xiaoai_asr 模式不需要 ASR 大模型，但包内含，体积可控）
#   URL 见下方"模型包"节，解压到 ./models/
```

## 3. 上传到群晖并启动

> ⚠ **fork 深度演进，代码烤进镜像**：kws/api_server/openai.py/tools 等改动不会进 bind-mount，
> 拉上游镜像或纯 `restart` 会丢功能（喊「停止聆听」时 `disable_listening()` 在镜像里缺失 → `AttributeError`）。
> 镜像构建走 [REBUILD.md](REBUILD.md)（**推荐路径 A：WSL 构建后推 NAS**；路径 B 为群晖本地构建）。本节只负责 bind-mount 文件与启动。

```bash
# 1) 部署 bind-mount 文件（config.py 已含 T7.6 关键词与路由钩子；.env 不入库需自备）
#    ⚠ scp 必须 -O：DSM 的 SFTP 视图受限（chroot），系统路径报 No such file or directory
ssh zxsadmin@10.10.10.2 'mkdir -p /volume2/docker/open-xiaoai-bridge'
scp -O -r config.py .env zxsadmin@10.10.10.2:/volume2/docker/open-xiaoai-bridge/
# 2) 镜像由显式 `docker build -t open-xiaoai-bridge:home` 提供（推荐路径 A：WSL 构建后推 NAS，见 REBUILD.md §1）
#    —— 不 scp 本机 docker-compose.yml：NAS 部署目录的 compose 已固定引用 image: open-xiaoai-bridge:home（无 build 段），
#       与 PC 同源；盲目覆盖可能冲掉 NAS 侧专有调整。
# 3) 重建并启动（models/ 经 bind-mount 提供，已在部署目录；容器启动重编 keywords.txt）
ssh zxsadmin@10.10.10.2 'cd /volume2/docker/open-xiaoai-bridge && /usr/local/bin/docker compose up -d'
```

## 3.5 ⚠ 设备侧缺陷：冷启动时钟卡固件构建日 ⇒ 一次断电即失联

> ⚠️ **本节只描述缺陷与修复方案，未对设备做任何改动。**
> 脚本存于 [`deploy/lx06/timefix.sh`](../../deploy/lx06/timefix.sh)（说明：[`deploy/lx06/README.md`](../../deploy/lx06/README.md)），
> **何时施工由用户决定**。
>
> 取证：逆向侧 `D:\_work\dev\reverse\lx06-firmware-reverse\run\clock-desync-boot-deadlock.md`
> 安全通告：`docs/plan/security-advisory-lx06-local-dos-and-creds.md` §15

**问题**：LX06 的 RTC 无效（`rtc0` = `2015-01-01`），`/etc/init.d/boot` 的 `time_init_buildtime()`
每次冷启动都把系统时钟设成**固件构建日**（`2025-09-17`）。该时间早于云端证书 `notBefore`
（`2026-06-25`）**9 个月** ⇒ 所有 HTTPS 报 `certificate is not yet valid` ⇒
拿不到云端校时 ⇒ **死锁**。后果：

- 云端 HTTP 成功数 **38 → 0**
- `mico_aivs_lab` 卡在 `wait for time sync`，`usock/` 不创建 ⇒ **语音全链路不可用**
- 第 4 步的 `curl https://gitee.com/...` **会直接失败**

**修法（备选，未部署）**：用**明文 HTTP `Date:` 头**校时（不校验证书，坏时钟下可用），挂到 `/data/init.sh`。
完整脚本见 `deploy/lx06/timefix.sh`，此处不重复贴全文。

> **判据用「与 `Date:` 头偏差 > 3600s」**，不要写「年份 < 2026」——后者 2027 年即失效。
> busybox `date` **没有 `-f`**，必须用 `-D FMT -d TIME`。设备**没有 `base64`/sftp-server**，
> 传文件用 `cat > file` 管道。

## 4. LX06 升级 client 并切换 server 指向

> ⚠ 需要把音箱上的 client 从「官方版」升级为「coderzc fork 版」——官方 client 不支持鉴权 token，
> 而 bridge 的 `OPEN_XIAOAI_TOKEN` 需要 client 端携带同值 Bearer（fork 版从 `/data/open-xiaoai/token.txt` 读取）。

```bash
ssh -o HostKeyAlgorithms=+ssh-rsa root@10.10.10.20

# 1) 更新 server 指向群晖 bridge
echo 'ws://10.10.10.2:4399' > /data/open-xiaoai/server.txt

# 2) 写入与 server 端一致的鉴权 token（deploy/.env 里的 OPEN_XIAOAI_TOKEN）
echo '<与.env相同的OPEN_XIAOAI_TOKEN>' > /data/open-xiaoai/token.txt

# 3) 用 fork 版 init.sh 重装 client（官方 init.sh 拉的是官方二进制，无 token 逻辑）
curl -sSfL https://gitee.com/coderzc/open-xiaoai/raw/main/packages/client-rust/init.sh | sh

# 4) 若需开机自启，重下 boot.sh 并重启
curl -L -o /data/init.sh https://gitee.com/coderzc/open-xiaoai/raw/main/packages/client-rust/boot.sh
reboot
```

> 注意：重装后 `/data/open-xiaoai/server.txt` 与 `token.txt` 若被 init.sh 覆盖需重新写入；
> 官方版与 fork 版协议同源（AppMessage 四件套），切换无兼容性风险。

## 5. 验收清单（按序）

0. **设备时钟正确**（若已断电过则需先处理 §3.5）：`ssh root@10.10.10.20 date` → 当前年月；`ls /tmp/mico_aivs_lab/usock/` → 有 `speech.usock`

1. `curl http://10.10.10.2:9092/api/health` → 200
2. 浏览器打开 **http://10.10.10.2:9092/admin** → 输入 `.env` 里的 `ADMIN_TOKEN` → 总览页各卡片正常、`MONITOR_SERVICES` 配置的外部服务显示在线
3. `docker logs -f open-xiaoai-bridge` → 出现音箱连接 + `get_version` 日志
4. 音箱喊 **「测试模式」** → 播报「桥接正常，家庭中枢在线」
5. 音箱喊 **「你好小塔」** → 播「我在」→ 问一句天气 → OpenAI 兼容大模型回答（当前 DeepSeek，可热切换；多轮追问验证上下文；喊「小爱同学」验证可打断）
6. 音箱喊 **「你好助教」** → 进入辅导人设（四年级导师，苏格拉底式引导、不直接报答案）；喊「你好小塔」切回后确认人格无串台
7. 音箱先喊 **「你好小塔」** 进对话（听到"我在"），再说 **「现在上海天气怎么样」** → 回答来自 weather 工具而非纯生成；bridge 日志见 `tool round: ['weather_get']` 回环。**注意：连说"小塔，…"无效**（唤醒词是完整「你好小塔」，KWS 词表无单独「小塔」）
8. HA 侧建一个临时脚本调 `POST http://10.10.10.2:9092/api/play/text`（body `{"text":"来自HA的播报"}`）→ 音箱说话
9. 音箱喊 **「停止聆听」** → 麦克风静音 + KWS 停止分析 + TTS「已停止聆听」（隐私开关，语音通道自关）；`curl -X POST -H "Authorization: Bearer <token>" http://10.10.10.2:9092/api/audio_input`（唯一恢复路径；token 取 `.env` `BRIDGE_API_TOKENS` 任一 label 的 token 值——现网无旧单值 `BRIDGE_API_TOKEN`，不带 Bearer = 401 token_missing）→ mic 恢复 on + KWS 恢复分析，返回 `{"success":true,"mic":"on","listening":true}`（2026-10-28 部署冒烟实测）

## 5.5 后台面板（:9092/admin）

> 无需 SSH 即可查看系统状况与日志、更换上游 AI 的接口地址（含端口）/模型规格/API Key。

- **入口**：`http://10.10.10.2:9092/admin`（局域网 / WireGuard 内可达），首次打开输入 `ADMIN_TOKEN`（存浏览器 localStorage）
- **总览**：bridge 运行态、小爱音箱、AI 对话后端（地址/模型/key 状态）、OpenClaw/QwenPaw、音频管线（VAD/KWS）、外部服务探测（`MONITOR_SERVICES` 可选配置——纯状态监控、与对话链路无关，监控谁由 .env 决定）
- **日志**：内存环形缓冲实时增量拉取（约 2000 条），级别过滤、自动滚动
- **设置**：
  - 「测试连接」先预检新 地址+Key（GET /models，兜底 chat ping），再保存
  - 「保存并生效」写入运行时覆盖层 `./data/runtime-overrides.json` 并热生效——下一次对话即用新配置，**无需重启容器**
  - 覆盖值优先级高于 config.py/.env；字段旁有「已覆盖」标记，「清除」回落底层值
- **安全**：所有 `/api/admin/*` 需 Bearer token；未配置 `ADMIN_TOKEN` 时接口整体拒绝；密钥读取只回掩码。**不接受 `?token=`**（查询串会进浏览器历史/access log），面板走 localStorage + `Authorization` 头
- 升级镜像时注意：`upgrade-image.sh` 重启容器不影响 `data/`（覆盖层持久化在宿主卷）

## 6. HA 侧 script 实体（免唤醒表引用）

> ⚠ 下表为**初版示意**。生产权威 = 工作区 `deploy/ha/`（按代际编号的 append 片段，含
> A7 智能播控路由版：`music_next/prev` 按「NUC-HiFi 在播→LMS / 音箱在播→xiaomusic /
> 都空闲→hifi_mode」三分支 choose，`music_stop` 双链路停）——改 HA 配置以那边为准，
> 本表仅说明 config.py 只认 script 名、HA 侧改实现不影响语音层的解耦原则。

| script 实体 | 内容（初版示意） |
|------------|-------------|
| `script.hifi_mode` | WOL 唤醒 NUC → 等 player 在线 → LMS randomplay → TTS「高保真模式已开启」（已上线） |
| `script.music_stop` | choose：NUC 在播→media_stop(nuc_hifi)；否则 xiaomusic 停止（双链路） |
| `script.music_next` / `music_prev` | choose 三分支路由（NUC→LMS 切歌 / 音箱→xiaomusic / 空闲→hifi_mode） |
| `script.music_vol_up` / `vol_down` | xiaomusic 音量（HIFI 链路音量归功放旋钮） |

config.py 只认 script 名，HA 侧改实现不影响语音层。

## 7. 免唤醒词表与调参

- 词表在 `config.py` 的 `APP_CONFIG["wakeup"]["keywords"]`（AI 唤醒词 + 全部免唤醒短语都在此）；
  免唤醒动作在 `DIRECT_COMMANDS`（两处必须同步，否则词表命中但无对应动作）
- 识别不灵：`kws.keywords_threshold` 下调；误触发：上调 + `vad.threshold` 上调
- **当前值（2026-09-24 收紧，incident §5 建议 3）**：
  `keywords_score=3.0`（原 2.0）· `keywords_threshold=0.35`（原 0.2）· `vad.threshold=0.4`（原 0.3）
- ⚠️ **`keywords_score`/`keywords_threshold` 改动需重启容器**（`SherpaOnnx.start()` 只在启动时建 KeywordSpotter；
  config 热重载只刷 `vad.threshold`/`min_silence`）；词表改动同理（`keywords.txt` 启动时编译）
  ✅ **已上线 2026-09-24**：新 config.py 同步至 NAS + 新镜像（含播放闸门/命中留证/caller 审计）推送 +
  `docker compose up -d --force-recreate`；启动日志确认 `KWS: [score:3.0, threshold:0.35], VAD:[threshold:0.4]`；

- **2026-10-26 白天实测（用户在场，生产 `p3-20261026`）**：24 个免唤醒词唯一命中 **10/24**；复测 6 句中对照组「停止音乐」亦未命中，VAD 对未命中句全部检出语音（2.4–3.2s）⇒ 当前 `score=3.0/threshold=0.35` 下召回不稳，与 2026-09-24 收紧的误触发取舍直接相关。调参需重启容器（同上）且需先评估 announce KWS 闸门对自触发回环的覆盖——**待裁决**，台账 `ws: docs/plan/pending-followups.md` §4。

### 音频输入（六麦 / 看门狗）

- `audio_input.channels`：采集声道数，默认 **6**（LX06 六麦）；bridge 自动同步到 native 的
  `AUDIO_INPUT_CHANNELS`，**改 1 即回退单声道**，client 不需重编。
- `audio_input.channel_strategy`：`strongest`（滑窗最强麦 + 迟滞，默认）/ `average`（等权平均）/
  `fixed`（固定 `audio_input.fixed_channel`）。
- `audio_input.watchdog_seconds`：连续无帧秒数，默认 **1.0**。client 的录音读循环 500ms 无数据即退出，
  阈值必须显著小于 3s；恢复动作是先停后启，失败按指数退避。
- 真机 A/B 与回滚判据见 `ws: .scratch/lx06-voice-hub-baseline/acceptance-checklist.md` §4/§5。

### PC 语音注入（可选，默认关闭）

- **默认关闭**：`inject.enabled=false` 且 `INJECT_ENABLE` 留空 ⇒ 不监听 `:9093`，
  `POST /api/inject`、`/api/inject/stream`、`/api/inject/relay`、`/api/inject/text`
  返回 503 + `code=inject_disabled`；主对话路径完全不受影响。
- **开启**：`INJECT_ENABLE=1` 或 `config.py` 的 `inject.enabled=true`（需重启）。
  开窗 = 闸门 + 中继 + 唤醒，三者同开同关；唤醒失败会整窗回滚。
- **hook/消费端**：`ws: scripts/pc-inject/README.md`；原理见
  `ws: docs/plan/firmware-asr-injection-feasibility.md` §6/§7。
- **回滚**：关闭开关并重启容器；卸载 hook 后确认原生唤醒与 bridge 均恢复。
- **播放事件面**：`event_plane.source` 默认 `context`，`shadow_sources=["client"]`
  做影子比对；native 源默认关闭（运行时镜像无 openssh-client，`native_cmd` 留空）。
  容器内验证 `_gate_kws` 落点齐全（`_run_steps` / `api/play/{text,url,file}`）、`/api/audio_input` 恢复接口正常。
  旧镜像自动保留可回滚；旧 config.py 备份为 `config.py.bak-20260924104901`。

### 延迟埋点 TurnTrace（可选，默认关闭）

- **默认关闭**：`turn_trace.enabled=false` 且 `TURN_TRACE_ENABLED` 留空 ⇒ 零日志、零行为变化。
- **开启**：`TURN_TRACE_ENABLED=1` 或 `config.py` 的 `turn_trace.enabled=true`（config 热重载即可生效）。
  每轮对话只输出**一条** `[TurnTrace]` INFO 数值行：ASR final→LLM、LLM 耗时、TTS 合成、
  play_url→起播、播放时长、文案/回复字符数、`tool_rounds` 与 timeout/fallback 标志。
- **不含原话**：只记长度与耗时，不记 ASR 原文/回复文本；逐轮可据此算 P50/P95。
  缺失段记为 `-1.0`，未收尾的轮记为 `incomplete=1`。

- 短词（4 字以下）易误触发，优先用「下一首歌曲」而非「下一首」
- ⚠️ **播报文案不得含词表词**（否则形成自触发回环，见 `bridge/AGENTS.md` 「TTS 文案安全约束」
  及工作区 `docs/plan/incident-kws-self-trigger-loop.md`）
- 命中留证：
  - `data/kws_snapshots/kws_hit_*.pcm`（命中前 5 秒 16k/单声道，最多保留 10 个）
  - 日志行 `[KWS] 命中留证 | keyword=... vad_prob=... tokens=... ts=[...]`
  - （sherpa-onnx 不提供置信度，故以 tokens/timestamps 代替）
- 外部播报审计：`/api/play/{text,url,file}` 均记录 `caller=<IP>` 与消息摘要

## 8. 安全

- `.env` 不入库；`OPEN_XIAOAI_TOKEN` 已启用（client 连接需携带同值 Bearer）；**未配置该变量时服务端拒绝所有 client 连接（fail closed）**
- `BRIDGE_API_TOKENS` 具名业务 token 集合（`label:token,...`，与 `ADMIN_TOKEN` 分离；票 12）：配置后 `/api/play/*`、`/api/wakeup`、`/api/interrupt`、`/api/audio_input`、`/api/inject*`、`/api/tts/doubao` 必须带 `Authorization: Bearer <token>`；命中哪个 label 就在审计日志记 `label=`（调用方可定点吊销/轮换）。**未配置时这些写类端点返回 503（失败关闭）**，`/api/health`、`/api/status` 等只读端点仍可用；**配置非法（格式错/label 重复/值重复）时整份 503 `auth_config_invalid`**，细节看服务端日志
  - `BRIDGE_API_TOKEN`（旧单值）已收口：**2026-10-24 迁移完成并删除 legacy**（`.env` 仅存 `BRIDGE_API_TOKENS`）。
    回滚支点 = 镜像 `rollback-pre12` 标签 + NAS `.env.bak-20261024-pre12`（含旧值，0600）；完整模型与迁移步骤见 `ws: .scratch/lx06-voice-hub-followups/notes/10-token-model.md` §6
  - **升级顺序**：① 先生成 token 并给所有调用方（HA `nuc_tts`/rest_command、企业微信服务、脚本）加上 `Authorization: Bearer` 头；② 再写入容器 `.env` 并 `docker compose up -d` 生效（设置即强制）
  - 注意：企业微信/HA 调用方若仍按无鉴权调用，会在切换后收到 401（缺头）/403（错 token）
  - ⚠️ **HA 正在升级/改配置时不要先设置 token**：先等 HA 侧调用方改完并验证，再设置
    `BRIDGE_API_TOKENS`；否则 HA 的播报/恢复端点会 401/503，且升级窗口内难以区分是 HA 还是 token 问题
- `ADMIN_TOKEN` 必须强随机（`openssl rand -hex 16`）；面板可改上游 API 配置，泄露等同泄露 API Key
- **审计调用方**：日志里的 `caller=` 默认是 TCP peer 地址；`X-Forwarded-For` / `X-Real-IP`
  **默认不信任**（当前部署无反向代理，该头可由任意客户端伪造）。将来在 bridge 前面加反代时，
  才设 `TRUST_PROXY=1` 让审计记录 XFF 里的原始客户端
- **写类放行审计**（票 12）：写类请求放行时记一行 INFO `放行 <method> <path> label=<命中标签> caller=<peer>`；
  拒绝行带 `code= token_missing|token_invalid|auth_unconfigured|auth_config_invalid label=none`。
  日志永不出现 token 明文
- **9093 数据面**（票 12）：`INJECT_ENABLE=1` 不再等于可裸奔——未配 `INJECT_ALLOW_CIDR`
  且未显式 `INJECT_ALLOW_ANON=1` 时**拒绝监听**（启动日志 `inject data plane: auth=refused`）。
  启用注入时建议 `INJECT_ALLOW_CIDR=10.10.10.20/32`（设备 /32；DHCP 漂移需静态租约）；
  共享密钥首包鉴权待 hook 改动落地（`auth=key` 预留）。连接拒绝/替换均有 WARNING；
  当前模式可在 `GET /api/inject` 与 `/api/health` 的 `relay.auth` 字段核对
- 4399/9092 仅在局域网（host 网络 + 群晖防火墙），勿做公网端口映射；外网访问走 WireGuard
- HA Token 只进 `.env`，任何文件不得硬编码

## 模型包

VAD+KWS 模型 release：https://github.com/coderzc/open-xiaoai-bridge/releases/tag/vad-kws-asr-models
（WSL2 下载若慢，可临时走路由器透明代理；下载后 `unzip` 到 `models/`）
