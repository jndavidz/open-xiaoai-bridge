# 待定事项汇总（原文 6 项 · 现状：全部已并入 README 或结案）

> 本文的「待办 / 授权 / 排序」已并入 [`README.md`](README.md)（**项目侧计划唯一权威**）；本文只保留**深度取证与方案细节**，过时断言已在 `README.md` §7 取代链登记。
> 本文件是这些待定事项的**历史裁决入口**（深度取证分散在各文件）。
> ⚠️ **它已不是待办索引** —— 现行唯一清单是 [`README.md`](README.md) §2（编号 B-01…B-10），
> 下表 §0 给出逐项去处的映射。新增待定事项**写入 `README.md`**，不再追加到本文件。

---

## 0. 一览表

> **逐项去处**（2026-10-20 收敛）：
>
> | 原 # | 去向 |
> |---:|---|
> | 1 `InjectRelay` 丢文本 | → [`README.md`](README.md) **B-09** |
> | 2 TTS 文本转义 | → [`README.md`](README.md) **B-04**（TTS-E1 已真机定案：**报错非截断**） |
> | 3 `set_mic` 语义反转 | → [`README.md`](README.md) **B-02**（并需拍板 T7.6 期望） |
> | 4 时钟死锁 | → [`README.md`](README.md) **B-10**（`ping/time` 已实测**无鉴权**，且与 `timefix.sh` 是两步互补） |
> | 5 `chen@beijing` 公钥回归 | → **结案**：root 口令为公开默认值 ⇒ 不扩大攻击面，按「仅内网」基线不投入（`README.md` §2 台账） |
> | 6 `api_server.py` 工作区改动 | → 并入 **B-09**，不再单列 |

| # | 事项 | 类型 | 授权需求 | 建议 | 深度文档 |
|---:|---|---|---|---|---|
| 1 | `InjectRelay` WS 晚连丢文本（`text_buffer` 重放） | bridge 代码 | 无需 | **修**（先修顺序陷阱） | [`inject-relay-text-buffer-replay.md`](inject-relay-text-buffer-replay.md) |
| 2 | 🔴 TTS 文本 JSON 转义缺陷 | bridge 代码 | 无需 | **修**（10 行，注意顺序） | [`tts-text-escaping-fix.md`](tts-text-escaping-fix.md) |
| 3 | `set_mic` 语义反转 | bridge 代码 | 无需 | **先确认 T7.6 期望** | [`../plan/lx06-set-mic-semantics-reversed.md`](../../../docs/plan/lx06-set-mic-semantics-reversed.md) |
| 4 | 时钟死锁（`timefix.sh` 未部署） | 部署资产 | **需** | **改用 `TimeSyncAPI`** | [`clock-deadlock-resolution.md`](clock-deadlock-resolution.md) |
| 5 | `chen@beijing` 公钥覆盖回归 | 设备侧配置 | **需** | **恢复出厂公钥** | [`../plan/lx06-ssh-key-injection-deviations.md`](../../../docs/plan/lx06-ssh-key-injection-deviations.md) |
| 6 | `api_server.py` 工作区改动去留 | bridge 代码 | 无需 | **保留并提交** | 本文件 §6 |

**分组建议**：

- **可立即做（无需授权、纯 bridge 代码）**：#1 #2 #6
- **需先问你一句话**：#3（T7.6 真实期望是什么）
- **需授权（动设备）**：#4 #5

---

## 1. `InjectRelay` WS 晚连丢文本

**现状**：`core/services/api_server.py` 工作区已有 `text_buffer` 改动，**未提交**
（你此前只授权「建议落到 `bridge/docs/plan`」，未授权改 bridge 代码）。

**问题**：`publish_text()` 只做实时广播，而 `text_queues` 的订阅者是
**WS 连接时才创建** ⇒ 订阅晚于 ASR 结果产生时，那段文本**永久丢失**。
判据：bridge 日志 `[InjectRelay] texts=N`（N>0）但 WS 客户端什么都没收到。

**这是与 `asr-input-reliability.md`「monitor 重启窗口」并列的第二个独立根因。**

⚠️ **已知缺陷**：当前工作区改动把 `push_task` 建在重放**之前**，
`await ws.send_json(...)` 会让出控制权 ⇒ 实时队列可能插在重放中途 ⇒ **乱序**。

**建议**：修掉顺序陷阱（重放挪到 `push_task` 之前）后提交。

**完整取证与正确写法** → [`inject-relay-text-buffer-replay.md`](inject-relay-text-buffer-replay.md) §3.2

---

## 2. 🔴 TTS 文本 JSON 转义缺陷

**问题**：`core/services/speaker.py:75` 只做 **shell 单引号转义**，
而设备侧 `/usr/sbin/tts_play.sh:5` 把 `$1` **直接插进 JSON 字面量**：

```sh
ubus call mibrain text_to_speech "{\"text\":\"$1\",\"save\":1}"
```

⇒ 文本含 `"` / `\` / 换行时 **JSON 破裂**：

```
输入: 他说"你好"然后走了
命令: ubus call mibrain text_to_speech '{"text":"他说"你好"然后走了","save":1}'
                                          ^^^^ 字面量在这里结束
```

后果：ubus 解析失败（`exit 1`）或文本被**静默截断**。

**触发条件**：TTS 文案含英文双引号、反斜杠或换行。
**为何一直没暴露**：播报文案多为中文短句 ⇒ 属**潜伏缺陷**。

**安全域**：`blocking=False` 分支**安全**（走 `json.dumps`）。
⇒ 只有 `blocking=True` 这条路有洞。

⚠️ **修法顺序不能反**：必须先 JSON 转义、后 shell 转义，反了会被 shell 层破坏。

**完整取证与两种修法** → [`tts-text-escaping-fix.md`](tts-text-escaping-fix.md)

---

## 3. `set_mic` 语义反转

**问题**：`set_mic(on=True)` 实际**关闭**麦克风，`set_mic(on=False)` 实际**打开**
——与命名和 docstring 的表述**相反**（真机实证）。

**根因**：`speaker.py:143-146` 的 `event=7` 创建 `/tmp/mipns/mute`（静音）、
`event=8` 删除（解除静音），而调用侧把两者映射写反了。

**更严重的是**：`api_server.py:725-726` 有一段**因果写反的注释**，
⇒ 后人会按错误因果继续推导。

⚠️ **动手前必须先确认 T7.6 真实期望**：
- 「只要 KWS 停」→ 当前行为已满足，但**必须改掉错误日志/注释**，
  否则这是一个**误导用户的隐私开关**；
- 「要设备级静音」→ **必须修**，选方案 B 最稳。

**三方案对比 + 同源疑点（`wake_up(awake=False)` 也走同一对 event）**
→ [`../plan/lx06-set-mic-semantics-reversed.md`](../../../docs/plan/lx06-set-mic-semantics-reversed.md)

---

## 4. 时钟死锁（`timefix.sh` 未部署）

**现状**：`deploy/lx06/timefix.sh` 已写好但**未部署**，缺陷仍在。

**根因（2026-10-19 确证，比原推测更精确）**：

```
mico_aivs_lab 启动
  └─ 等 access("/tmp/ntp.status")          ← tmpfs，掉电即失
     且 access("/data/status/config_done")  ← ubifs，持久
```

`/tmp` 是 tmpfs ⇒ 每次冷启动 `ntp.status` 必然消失。
开机到 NTP 成功的窗口内 `sys_time_synced_wait()` **永久自旋** ⇒ AIVS 完全不可用。
若 NTP 永失败（RTC=2015 + 固件构建日早于云端证书 `notBefore` 的先有鸡先有蛋），
**永远等不到**。

**三条路线对比与推荐** → [`clock-deadlock-resolution.md`](clock-deadlock-resolution.md)

**⭐ 推荐**：改用云端 `TimeSyncAPI::request`（`https://api2.mina.mi.com/ping/time`）——
**不写 `/data`、不改 `init.sh`**，是唯一符合「不改设备状态」铁律的方案。
（`timefix.sh` 要写 `/data/init.sh`，正是此前被你否决过的同类操作。）

---

## 5. `chen@beijing` 公钥覆盖回归

**问题**：`/data/init.sh` 的 hook 用 **`cp -f`（覆盖）**而非 append，
出厂 squashfs 预置的 `chen@beijing` 公钥**每次重启被冲掉**。

**取证**：

| 位置 | 内容 | md5 |
|---|---|---|
| 出厂 squashfs | `chen@beijing`（唯一 1 行） | `293144b0…` |
| 设备 `/data/etc/...` | 只剩新注入的 RSA 钥 | `75783974…` |

**影响判断**：因 root 口令是公开默认值 `open-xiaoai`，
同网段攻击者本就能直登 root ⇒ **该回归不扩大攻击面**，
只**收窄你自己的合法访问路径**。

**三选项（推荐 A：恢复出厂公钥 + hook 改合并语义）**
→ [`../plan/lx06-ssh-key-injection-deviations.md`](../../../docs/plan/lx06-ssh-key-injection-deviations.md) §3

**顺带**：设备 dropbear host key 每次重启重新生成 ⇒ 客户端反复重认指纹
⇒ **「指纹突变」这个 MITM 告警信号失效**。当前不影响使用，但值得一并处理。

---

## 6. `api_server.py` 工作区改动去留

**现状**：

```bash
$ git diff --stat
 core/services/api_server.py | 11 +++++++++++
```

这处改动**就是第 1 项**的 `text_buffer` 重放实现。

**为什么一直没提交**：你此前只授权「建议落到 `bridge/docs/plan`」，
**未授权改 bridge 代码**。

**两个选项**：

| 选项 | 做法 | 代价 |
|---|---|---|
| **保留并提交**（推荐） | 先修 §1 的顺序陷阱，补一次容器/真机验证，再 commit | 需一次验证 |
| 回退 | `git checkout -- core/services/api_server.py` | 丢掉第 1 项的修复 |

---

## 7. 附：设备侧 SSH 连接方式（供施工参考）

WSL 侧已固化到 `~/.ssh/config.lx06-ir`：

```
Host lx06
    HostName 10.10.10.20
    User root
    IdentityFile ~/.ssh/lx06-ir/id_rsa
    IdentitiesOnly yes
    HostKeyAlgorithms +ssh-rsa      # dropbear 2017.75 只认 ssh-rsa
    PubkeyAcceptedKeyTypes +ssh-rsa
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
```

用法：`ssh -F ~/.ssh/config.lx06-ir lx06 '<命令>'`

⚠️ **红外执行侧当前跑在 WSL 宿主**（NAS→LX06 被 AX6S 单向阻断），
修好 AX6S 后只改 `LX06_HOST` 即搬回 NAS。该问题在另一对话处理。
