# client-rust：音箱侧补丁程序

> 音箱上跑的 `client` 二进制是什么、协议契约、版本谱系（idootop 原版 vs coderzc fork vs 本工作区部署）、部署与更新判据都在本文。
> 触发场景：排查 client ↔ bridge 连接/握手问题、升级或重装 client、评估 client 侧行为（事件源、shell RPC）对服务端的影响。

## 1. 定位与职责边界

client-rust 是刷机后跑在音箱（LX06 等，OpenWrt + busybox，ARMv7）上的唯一常驻补丁程序，通过明文 WebSocket 连接 bridge。它只做四件事（上游 README 原文）：

1. 转发麦克风输入音频流到 Server 端
2. 转发设备事件到 Server 端（语音识别结果、播放状态等）
3. 响应 Server 端指令调用（执行脚本、播放音频流、系统升级等）
4. 承载 Client/Server 共享通信协议

**业务逻辑一律在服务端**（bridge 的 Python 层）——这是设计原则：音箱内存算力有限，client 保持极薄。因此 bridge 的对话编排、VAD/KWS/ASR、TTS 管线全部不需要改 client。

固件层（刷机补丁 `client-patch` / USB 刷机工具）是另一套东西，本文不覆盖；刷机后 client 二进制与固件解耦，可独立替换升级。

## 2. 通信契约（bridge 对接必知）

- **通道**：明文 WebSocket `ws://<音箱可达地址>:4399`，服务端由 bridge 的 `native/src/server.rs` 提供。
- **消息协议**：`AppMessage` 四消息枚举（`Request` / `Response` / `Event` / `Stream`，定义在 `packages/client-rust/src/services/connect/data.rs`）——文本帧承载 JSON，二进制帧承载 `Stream`（音频裸字节，PCM 16k）。
- **握手**：coderzc fork 起支持 `Authorization: Bearer <token>` 头（环境变量 `OPEN_XIAOAI_TOKEN`），bridge 侧 `server.rs` 校验，失败回 401。
- **能力面**：RPC 含 `start_recording` / `stop_recording` / `run_shell` 等——client 默认提供任意脚本执行能力，**鉴权不配则等于裸奔**，只可连可信 Server。

## 3. 版本谱系

三个层面逐一对照（2026-10 实测 `diff -r` 两个仓库 HEAD 的 `packages/client-rust/`，置信度高）：

| | idootop/open-xiaoai 原版 | coderzc/open-xiaoai fork | 本工作区部署 |
|---|---|---|---|
| 状态 | **已归档**（README 顶部 WARNING） | 随 bridge 持续维护 | LX06 上跑 coderzc fork 的**预编译** client |
| 鉴权 | 无（明文裸协议） | `OPEN_XIAOAI_TOKEN` Bearer（**可选**，空 = 退回明文） | 已配 `token.txt`（token 鉴权） |
| 预编译下载源 | `gitee.com/idootop/artifacts/...` | `gitee.com/coderzc/open-xiaoai/releases/...` | 同 coderzc |
| 核心机制 | AppMessage / FileMonitor / SpeakerManager | **与原版逐字节一致** | 同 coderzc |

**fork 的 diff 只有 4 个文件**，`src/` 下仅 `bin/client.rs`：

- `client.rs`（+13 行）：`connect()` 读 `OPEN_XIAOAI_TOKEN`，非空则握手加 `Authorization` 头。
- `boot.sh` / `init.sh`：下载源改 coderzc；`.tmp` + `mv` 原子写（下载失败回退旧版）；读 `/data/open-xiaoai/token.txt` 并 export；`init.sh -u` 仅更新不启动。
- `README.md`：补 token 与更新章节。

**本工作区未对 client 做任何本地修改**。client 是上游可替换件，协议与 idootop 原版同源，任一版本可互换降级/升级。

## 4. 部署与更新

设备上三个文件即全部状态（`/data/open-xiaoai/`）：

| 文件 | 作用 |
|---|---|
| `server.txt` | bridge 地址（如 `ws://<群晖IP>:4399`），以设备上实际值为准 |
| `token.txt` | 鉴权 token，须与 bridge `OPEN_XIAOAI_TOKEN` 一致 |
| `client` | 二进制本体 |

启动链：`/etc/rc.local:4`（只读 squashfs，重绑定动不了它）→ `sh /data/init.sh` → 读 server.txt/token.txt → kill 旧进程 → 拉起 `client`。

⚠ **`/data/init.sh` 在本工作区是双职责文件**：除了 client 自启，还承担 SSH 免密的 dropbear bind-mount（设备 `/` 只读且无 overlay，`/etc/dropbear` 只能靠 `mount --bind /data/etc/dropbear` 持久化）。任何一侧单独覆盖都会打断另一侧——合并版见 §6 与工作区 `deploy/lx06/init.sh`。

更新判据：

- `boot.sh` **仅当本地无 `client` 时才下载**——直接重跑 boot.sh 不会升级，升级走 `init.sh -u` 或手工替换二进制。
- 设备 busybox 无 `nohup`；启动脚本自带 kill 旧进程，重复拉起安全。
- 预编译资产的真实 URL 是 `https://gitee.com/coderzc/open-xiaoai/releases/download/open-xiaoai-client/client`（**带 `/client` 资产名**）；只写到 release tag 那一级的形式实测 404（2026-10-21）。下载后校验：`file`/头 20 字节应为 ELF32 LE ARM `ET_DYN`（设备无 `od`，用 `hexdump -v -e`）。

## 5. 已知缺陷与排障判据

| 缺陷 | 影响 | 处置 |
|---|---|---|
| ASR 事件 = 10ms 轮询 tail 设备日志，**启动丢历史** | 事件源整体延迟与丢帧风险，client/bridge 重启窗口内的 ASR 文本丢失 | 机制与取证见 [AGENTS.md「ASR 事件的真实来源」](../AGENTS.md)；服务端侧对策见 `docs/plan/asr-input-reliability.md` |
| 鉴权可选、默认明文 | token.txt 缺失即退回无鉴权 + run_shell 裸协议 | 部署时必配 token.txt；网段隔离（勿与访客 WiFi/IoT 同段） |
| **无独立版本号** | 排障时无法从二进制确认构建版本 | 只能靠下载源/部署记录区分；重装即换 coderzc 最新 release |
| 4399 连接是单一长连接 | client 掉线期间所有事件/音频中断，仅靠重连恢复 | 排障第一步先确认设备 4399 ESTABLISHED（判据见 AGENTS.md「kws.log 是死通道」节） |
| `run_shell` 只透传设备脚本退出码，而设备脚本自身退出码不可信 | `/api/play/text`（blocking）声音已出却恒返回 `{"success": false}`——`speaker.py` 以 `exit_code == 0` 判定，而 `/usr/sbin/tts_play.sh` 末行 `[ x"$player_stat" == x"1" ] && mphelper play` 在空闲时 `player_stat=0` ⇒ rc=1 | 2026-10-21 真机 `time /usr/sbin/tts_play.sh` 实测 rc=1/≈1s 与 bridge 1.69s 对齐；判成功改看非阻塞路径与 `playing` 事件，勿信 blocking 返回值 |

## 6. 2026-10-21 事故与恢复实录：client 文件整体丢失

**现象**：音箱「你好贾维斯」等自定义唤醒词全无响应，而设备原生「小爱同学」正常。bridge 侧 `speaker.ready` 虽 true，但 4399 无连接 ⇒ 麦克风音频、`run_shell` 设备控制全断。

**根因**：不是「client 没启动」，是**设备上根本没有 client**。

1. 2026-10-19 22:10 设备重绑定清掉了 open-xiaoai 的两个落点：`/data/init.sh`（含 `.orig` 备份）与 `/data/open-xiaoai/` 三件套。
2. 当晚为修 SSH 免密重建的 `/data/init.sh` **只写了 dropbear bind-mount 一段**，把 boot.sh 的自启链顶掉了 ⇒ 此后重启永不自启（10-21 07:15 设备重启过、仍无 client，实证）。

**重绑定是选择性删除，不是格式化 `/data`**（关键判据，恢复其它补丁产物时同样适用）：
- 存活：`/data/etc/device.info`、`/data/etc/miio.cfg`、`ai-crontab/`、`notify/`、`timer/`、`bt/` 仍是 Sep 17 2025 时间戳；`/etc/rc.local` 调用点在。
- 重写：`TOKEN`、`cacerts/`、`miio/`、`idm/`、`voip/`、`etc/binfo`、`etc/messaging.cfg` 集中在 10-19 22:09–22:10 = 重新配网。

**证据边界**：设备 syslog 轮转最远只回溯到 10-20 13:45（`messages.15.gz`），22:10 那一次无日志留存 ⇒ 「`/data/open-xiaoai/` 由重绑定删除」是**同窗口两个已知落点同时消失**的时间线推定，未取到直接删除记录。

**恢复动作**（2026-10-21，全部实测通过）：
- 放 fork 版二进制（851344B，md5 `b12ebe6e8289de3adcdba6b5641eb843`）、`server.txt`、`token.txt`（0600，经 stdin 直传不落聊天）。
- `/data/init.sh` 换成 **合并版**（A 段 dropbear bind-mount 原样保留 + B 段 client 自启），源件留档在工作区 [deploy/lx06/init.sh](../../deploy/lx06/init.sh)；旧文件备份 `/data/etc/init.sh.bak-20261021-dropbear-only`。
- ⚠ **不要照抄 [deploy/README.md](../deploy/README.md) §4 第 4 步**（`curl -o /data/init.sh .../boot.sh`）——那会覆盖 dropbear 钩子、SSH 免密当场失效。

**端到端验证**：`[KWS] 命中留证 keyword='你好贾维斯'` → `🔥 触发唤醒` → `before_wakeup returned: openai` → 小爱 native ASR `recognized` → `Bridge 接管原生 ASR`，用户确认可唤醒并连续对话。

**顺带定位的一个诊断盲区**（非 client 缺陷，但会误判成 client 故障）：「停止聆听」置位后 KWS 全程静默，若日志级别恰在 INFO，DEBUG 级 `[KWS] 检测到语音` 不落盘 ⇒ 看起来像「音频没上来」。`POST /api/audio_input` 会连带执行 `kws.enable_listening()`（[api_server.py](../core/services/api_server.py) `handle_audio_input`），但它先 enable 再回报 `listening`，不回显调用前状态——排障时要**先单独查 `is_listening_disabled()` 再决定清理**。

**遗留**：合并版 init.sh 只经 `sh /data/init.sh` 手工执行验证，**尚未做真实 reboot 验开机自启**——备份文件保留到该项通过后再删。

## 参考

- 上游 README（安装/编译原文）：https://github.com/coderzc/open-xiaoai/blob/main/packages/client-rust/README.md
- 原版 README：https://github.com/idootop/open-xiaoai/blob/main/packages/client-rust/README.md
- 设备端命令与行为（播放通道、tts_play.sh、中断处理）：[AGENTS.md「音箱设备控制命令」](../AGENTS.md)
- 服务端侧架构：[README.md 系统架构](../README.md#系统架构)
