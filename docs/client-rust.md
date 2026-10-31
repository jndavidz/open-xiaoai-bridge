# client-rust：音箱侧补丁程序

> 音箱上运行的 client 二进制：职责边界、协议契约、版本谱系、部署与更新判据、已知缺陷。
> 触发场景：4399 连不上 / 401、升级或重装 client、评估 client 事件源与 shell RPC 对服务端的影响、恢复设备补丁产物。

## 1. 定位与职责边界

client-rust 是刷机后跑在音箱（LX06，OpenWrt + busybox，ARMv7）上的常驻补丁程序，以明文 WebSocket 连接 bridge。它只做四件事：

1. 转发麦克风音频到服务端
2. 转发设备事件（ASR 文本、播放状态）
3. 响应服务端 RPC（执行脚本、播放、录音、系统升级等）
4. 承载 Client/Server 共享通信协议

业务逻辑全在 bridge 的 Python 层：对话编排、KWS/VAD/ASR、TTS、托管播放都不需要改 client。client 是上游可替换件，本工作区不维护其源码，也不做本地修改。

**RPC 面**（上游 HEAD 注册的命令）：`get_version`、`run_shell`、`start_play`、`stop_play`、`start_recording`、`stop_recording`。`run_shell` 用 `/bin/sh -c` 执行任意脚本，client 又以 root 运行 ⇒ 等价 root，只可连可信 bridge。

**内部三个 monitor**：

- instruction：tail `/tmp/mico_aivs_lab/instruction.log` → ASR 事件；
- playing：每 10ms 执行一次 `mphelper mute_stat` → 播放状态事件；
- kws：监听 `/tmp/open-xiaoai/kws.log`；本设备该目录不存在，monitor 空转、不产生事件（唤醒由 bridge 的 sherpa KWS 完成）。

固件层（刷机补丁、USB 刷机工具）不在本文范围；client 与固件解耦，可独立替换升级。

## 2. 通信契约

- **通道**：明文 WebSocket `ws://<bridge>:4399`，服务端为 bridge 的 [native/src/server.rs](../native/src/server.rs)。
- **消息**：`AppMessage` 四型 `Request` / `Response` / `Event` / `Stream`（上游 `packages/client-rust/src/services/connect/data.rs`）。文本帧承载前三型的 JSON；**二进制帧承载 `serde_json` 序列化的 `Stream`**——内容仍是 JSON（`bytes` 是整数数组），不是裸 PCM。
- **音频**：client 默认 1ch / 16k / 16bit（上游 `audio/config.rs`）；bridge 通过 `start_recording` 下发 `channels`（生产默认 6）、`pcm=noop`、period/buffer 等参数。
- **鉴权**：fork 版 `connect()` 读环境变量 `OPEN_XIAOAI_TOKEN`，非空则加 `Authorization: Bearer`。bridge 侧未配置该变量时**拒绝所有连接**（401，fail closed；token 常量时间比较）。设备 `token.txt` 必须与 bridge 环境变量同值。
- **能力面**：上游 token 可选，空值 = 不加头。token 漏配等于开放 root 脚本通道，部署必须配 token，只连可信 Server。

## 3. 版本谱系

2026-10-31 复核（两仓 HEAD：coderzc `14e1034`、idootop `bc3396c`，对 `packages/client-rust/` 做 `diff -r`）：差异仍只有 4 个文件。

| | idootop/open-xiaoai 原版 | coderzc/open-xiaoai fork | 本工作区部署 |
|---|---|---|---|
| 状态 | 已归档（README 顶部 WARNING） | 持续维护 | coderzc fork 的预编译二进制 |
| 鉴权 | 无 | `OPEN_XIAOAI_TOKEN` Bearer，空则不加头 | 已配 `token.txt`；bridge fail closed |
| 下载源 | `gitee.com/idootop/artifacts/...` | `gitee.com/coderzc/open-xiaoai/releases/...` | 同 coderzc |
| 核心机制 | AppMessage / FileMonitor / SpeakerManager | 与原版逐字节一致 | 同 coderzc |

**4 个差异文件**：

- `src/bin/client.rs`：`connect()` 读 token，非空加 `Authorization` 头（唯一源码差异）；
- `init.sh`：下载源改 coderzc；新增 `-u/--update`（只更新不启动）；`.tmp` + `mv` 原子写；读 `token.txt` 并 export；
- `boot.sh`：下载源改 coderzc；`.tmp` + `mv`；读 `token.txt` 并 export；仍只在本地无二进制时下载，随后前台启动 client；
- `README.md`：补 token 与更新说明。

**版本识别**：

- 二进制没有 `--version`；`argv[1]` 一律当 WS 地址（传 `--version` 会去连它并挂起重连）。
- client 注册 RPC `get_version`，返回 Cargo 版本 `1.0.0`；原版与 fork 包版本相同，不含 commit/构建号；bridge 当前不主动查询 ⇒ 排障仍以 md5 和来源为准。
- 本地版本约定（2026-10-25 起）：**1.0 = idootop 原版，2.0 = coderzc fork（现部署）**，后续整数递增。
- **当前部署**（2026-10-31 真机核对）：`/data/open-xiaoai/client` = 851,344 B，md5 `b12ebe6e8289de3adcdba6b5641eb843`（2026-10-21 21:07 安装），与 gitee 当前 release 资产一致。

## 4. 部署与更新

设备状态只有三件（`/data/open-xiaoai/`）：

| 文件 | 作用 | 当前值 |
|---|---|---|
| `server.txt` | bridge 地址 | `ws://10.10.10.2:4399` |
| `token.txt` | 鉴权 token，与 bridge `OPEN_XIAOAI_TOKEN` 同值 | 0600 |
| `client` | 二进制本体 | 0755 |

**启动链**：`/etc/rc.local:4`（只读 squashfs，不可直接改）→ `sh /data/init.sh` 后台执行 → 分段完成 → 后台拉起 client。

`/data/init.sh` 是**多职责合并脚本**（工作区源件 [deploy/lx06/init.sh](../../deploy/lx06/init.sh)；2026-10-31 设备与工作区同 md5 `0bf481d65e6e648061608f98b073fd72`）：

- **A) dropbear 免密**：`/data/etc/dropbear` bind mount 到 `/etc/dropbear`；
- **C) shadow 加固**：`/data/etc/shadow` bind mount 到 `/etc/shadow`（出厂 root 口令已失效）；
- **B) client 自启**：等价 boot.sh + fork 的 token 读取；必须**后台**拉起（前台/exec 会让后续段永不执行）；
- **D) 关 dropbear 密码登录**（key-only）；
- 末尾还有：SSH 22 限源（NAS / 家庭 PC / 办公室 VPN）+ `:9022` 密码兜底实例 + B-10 时钟兜底（`/tmp/ntp.status` 缺失时后台校时）。

任何一段单独覆盖都会让其余段静默失效。改完先 `sh -n`，再重启并确认「执行到达」，不能只验文件内容。

**更新判据**：

- `boot.sh` / `init.sh` 默认调用只在本地无 `client` 时下载；直接重跑不会升级。
- 升级二进制：运行上游 `init.sh -u`（只更新 `/data/open-xiaoai/client` 后退出，不触碰 `/data/init.sh`），或手工下载到 `.tmp` → 校验 → `mv` → 重启 client。**不要**用 `boot.sh` 覆盖 `/data/init.sh`。
- 资产 URL：`https://gitee.com/coderzc/open-xiaoai/releases/download/open-xiaoai-client/client`（必须带 `/client`；只到 tag 级实测 404）。
- 校验：md5，且头 20 字节为 ELF32 LE ARM `ET_DYN`（`03 00 28 00` 结尾）；设备无 `od`，用 `hexdump -v -e`。
- ⚠ [deploy/README.md](../deploy/README.md) §4 第 4 步仍写着 `curl -L -o /data/init.sh .../boot.sh`；照做会覆盖合并版、丢掉 SSH 加固与自启链。以工作区 `deploy/lx06/init.sh` 为准。
- 设备 busybox 无 `nohup`；init.sh 自带 kill 旧进程，重复拉起安全。

## 5. 已知缺陷与排障判据

| 缺陷 / 机制 | 影响 | 当前处置与判据 |
|---|---|---|
| ASR 文本靠 10ms 轮询 `instruction.log`；启动时 `position = 文件末尾`，每次 WS 重连 monitor 重启都从末尾起 | 重启窗口内的 ASR 永久丢失；延迟地板 10ms | bridge 侧减少重连；历史无法补拉。取证与可做项见 [AGENTS.md「ASR 事件的真实来源」](../AGENTS.md) 与 [asr-input-reliability.md](plan/asr-input-reliability.md) |
| `playing` 靠 10ms 执行 `mphelper mute_stat`（每次新建 `/bin/sh`） | 设备进程创建频繁；精度受调度影响 | bridge 生产主源已切 native `play_status_broadcast_event`（`event_plane.source=native`，2026-10-26 起；client/native 影子并行至 2026-11-09）。native 实测快约 102ms，但 20 次中 4 次丢 `status:0`，超时兜底必须保留。client 自身轮询仍在设备上运行 |
| 录音读循环 500ms 无数据 → `break` 并自行 `stop_recording`，不发事件 | 采集静默死亡，唤醒词永久失效 | bridge 1s 无帧看门狗「先停后启」+ 指数退避；`XiaoAI.audio_health()` |
| 上游 token 可选，空值不加头 | 漏配 token = 无鉴权 + root shell | bridge fail closed；设备必配 `token.txt`。本机 SSH 已 key-only + 限源 + root 随机口令；**访客网段隔离仍未做** |
| 无构建版本号（见 §3） | 无法从二进制确认来源 | 按 md5 + 安装日期识别；沿用 1.0/2.0 约定 |
| 单一 4399 长连接 | 掉线期间事件/音频全断 | 排障第一步确认设备侧 4399 `ESTABLISHED` |
| `run_shell` 只回传脚本退出码，而设备脚本 rc 不可信（`tts_play.sh` 空闲时 rc=1） | 声音已出却判失败 | 阻塞播报主路径已改为「合成落盘 + `file://` + 回读」；`tts_play.sh` 仅兜底，按能力表的结构化 ok / rc∈{0,1}+stderr 判据 |
| RPC 先发送、后登记 pending | 极快响应可能先到被丢，表现为一次 10s 超时 | 查「响应已到但 pending 未登记」；客户端无修复，重试可恢复 |
| `kws.log` 通道不存在（见 §1） | 不产生 kws 事件 | 不要据此判 client 故障；唤醒走 bridge sherpa |
| 「停止聆听」后 KWS 全程静默，INFO 级日志看不到 `[KWS] 检测到语音` | 误判成音频链路 / client 故障 | 先查 `is_listening_disabled()`；`POST /api/audio_input` 是恢复路径，它先 enable 再回报 `listening`，不回显调用前状态（[api_server.py](../core/services/api_server.py)） |

## 6. 设备落点与恢复要点

| 落点 | 内容 |
|---|---|
| `/data/init.sh` | 多段合并启动脚本；权威源件在工作区 [deploy/lx06/init.sh](../../deploy/lx06/init.sh) |
| `/data/open-xiaoai/` | `client`、`server.txt`、`token.txt` |
| `/data/etc/dropbear/` | dropbear 主机密钥（持久化 bind mount 源） |
| `/data/etc/shadow` | 加固后的 shadow（bind mount 源） |
| `/data/etc/config/dropbear` | key-only 配置（bind mount 源） |
| `/data/etc/init.sh.bak-*` | 历史备份 |

**重新配网会选择性删除 `/data` 顶层**（不是格式化）：曾出现 `init.sh` 与 `open-xiaoai/` 三件套同时消失，而 `/data/etc/device.info`、`miio.cfg`、`ai-crontab/`、`notify/`、`timer/`、`bt/` 保留。恢复前先核对哪些落点在、哪些没了，再按段补齐。

**恢复顺序**：

1. 写入多段合并版 `/data/init.sh`（工作区副本），`sh -n` 语法检查；
2. 写入 `client` / `server.txt` / `token.txt`（token 0600，经 stdin 直传，不落聊天、不入日志）；
3. 重启设备，确认自启链：22 key-only 可登录、9022 兜底在、4399 `ESTABLISHED`、`/tmp/ntp.status` 重建、init.sh 后续段均执行到；
4. 语音链路验证：KWS 命中 → 唤醒 → 原生 ASR `recognized` → bridge 接管。

## 参考

- 上游 README（安装/编译原文）：https://github.com/coderzc/open-xiaoai/blob/main/packages/client-rust/README.md
- 原版 README：https://github.com/idootop/open-xiaoai/blob/main/packages/client-rust/README.md
- 设备端命令与行为（播放通道、`tts_play.sh`、中断处理）：[AGENTS.md「音箱设备控制命令」](../AGENTS.md)
- ASR 可靠性：[docs/plan/asr-input-reliability.md](plan/asr-input-reliability.md)
- 服务端侧架构：[README.md 系统架构](../README.md#系统架构)
