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

启动链：`/data/init.sh`（boot.sh）开机自启 → 读 server.txt/token.txt → kill 旧进程 → 拉起 `client`。

更新判据：

- `boot.sh` **仅当本地无 `client` 时才下载**——直接重跑 boot.sh 不会升级，升级走 `init.sh -u` 或手工替换二进制。
- 设备 busybox 无 `nohup`；启动脚本自带 kill 旧进程，重复拉起安全。

## 5. 已知缺陷与排障判据

| 缺陷 | 影响 | 处置 |
|---|---|---|
| ASR 事件 = 10ms 轮询 tail 设备日志，**启动丢历史** | 事件源整体延迟与丢帧风险，client/bridge 重启窗口内的 ASR 文本丢失 | 机制与取证见 [AGENTS.md「ASR 事件的真实来源」](../AGENTS.md)；服务端侧对策见 `docs/plan/asr-input-reliability.md` |
| 鉴权可选、默认明文 | token.txt 缺失即退回无鉴权 + run_shell 裸协议 | 部署时必配 token.txt；网段隔离（勿与访客 WiFi/IoT 同段） |
| **无独立版本号** | 排障时无法从二进制确认构建版本 | 只能靠下载源/部署记录区分；重装即换 coderzc 最新 release |
| 4399 连接是单一长连接 | client 掉线期间所有事件/音频中断，仅靠重连恢复 | 排障第一步先确认设备 4399 ESTABLISHED（判据见 AGENTS.md「kws.log 是死通道」节） |

## 参考

- 上游 README（安装/编译原文）：https://github.com/coderzc/open-xiaoai/blob/main/packages/client-rust/README.md
- 原版 README：https://github.com/idootop/open-xiaoai/blob/main/packages/client-rust/README.md
- 设备端命令与行为（播放通道、tts_play.sh、中断处理）：[AGENTS.md「音箱设备控制命令」](../AGENTS.md)
- 服务端侧架构：[README.md 系统架构](../README.md#系统架构)
