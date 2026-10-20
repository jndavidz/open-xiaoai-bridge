# 时钟死锁：三条路线对比与推荐

> 本文的「待办 / 授权 / 排序」已并入 [`README.md`](README.md)（**项目侧计划唯一权威**）；本文只保留**深度取证与方案细节**，过时断言已在 `README.md` §7 取代链登记。
> 状态：**待用户授权**（会改设备状态或发起云端请求）
> 索引：[`pending-decisions.md`](pending-decisions.md) §4
> 上游取证：`D:\_work\dev\reverse\lx06-firmware-reverse\run\libmico-common-system-runtime.md` §3
> **根因已于 2026-10-19 确证**（此前 `timefix.sh` 的注释是推测，现升级为确证）

---

## 1. 根因（确证版）

```
mico_aivs_lab 启动
  └─ "start to wait for time sync, and config complete."
       等 access("/tmp/ntp.status")            ← tmpfs，掉电即失
       且 access("/data/status/config_done")    ← ubifs，持久
       都满足才继续
       ├─ 满足: "NTP.STATUS EXIST, sys config is ok!"
       │         → "time sync & sys config all done !!!"
       └─ 否则:  sys_time_synced_wait() 永久自旋
```

**反汇编实证**（`libmico-common.so`）：

| 函数 | 地址 | 实现 |
|---|---|---|
| `sys_time_is_synced` | `0x1ad1c` | `access("/tmp/ntp.status")` |
| `sys_time_set_synced` | `0x1acf0` | `file_write_string("/tmp/ntp.status", ...)` |
| `sys_time_synced_wait` | `0x1ad40` | 循环 `sys_time_is_synced()`，`!=1` 继续等 |
| `sys_is_config_done` | `0x1adc4` | `access("/data/status/config_done")` |

**真机实测**（只读）：

```
/tmp/ntp.status              rw-r--r--  2 B   内容 "ok"    ← 当前已同步
/data/status/config_done     rw-r--r--  0 B   空标记文件   ← 持久
```

### 1.1 死锁如何形成

```
冷启动
  → /etc/init.d/boot 的 time_init_buildtime() 把时钟设为固件构建日（2025-09-17）
  → 该时间早于云端证书 notBefore
  → HTTPS 全部报 "certificate is not yet valid"
  → 无法连云校时
  → /tmp/ntp.status 永不出现（/tmp 是 tmpfs，启动即空）
  → mico_aivs_lab 永久等
  ⇒ AIVS 完全不可用
```

**先有鸡先有蛋**：要对时得先连云，要连云得先有正确时间。

---

## 2. 三条路线

### 路线 A：`timefix.sh`（已有，未部署）

**做法**：用**明文 HTTP 响应头的 `Date:`**（不校验证书）取真实 UTC 打破死锁。
纯 IP 优先，不依赖 DNS 就绪。偏差 > 3600s 才动时钟。

**已写好**：`deploy/lx06/timefix.sh`

**部署方式**：`/data/init.sh`（网络就绪后）调用，或手动 `sh /data/timefix.sh`。

| 维度 | 评估 |
|---|---|
| 有效性 | ✅ 高（不依赖证书，4 个明文源） |
| 是否写 `/data` | 🔴 **是**（`/data/init.sh` + `/data/timefix.log`） |
| 与你铁律的冲突 | ⚠️ **同类操作曾被否决**（时钟死锁排查中部署 `timefix.sh` 并改 `init.sh`，经你否决后已全部回退） |
| 额外风险 | 改 `init.sh` 会影响开机时序；`cp -f` 类 hook 已有前科（见 §5 公钥回归） |

### 路线 B：修 `/etc/init.d/boot` 的 `time_init_buildtime()`

**做法**：让它在时钟已合理时不覆盖。

| 维度 | 评估 |
|---|---|
| 有效性 | ✅ 高（根治，不只打破一次死锁） |
| 改动位置 | 🔴 **改只读 squashfs 里的 init 脚本** ⇒ 需重新打包固件或用 overlay |
| 可回滚性 | ❌ 差（改固件） |
| 建议 | **不推荐**——为一个小缺陷动固件，得不偿失 |

### 路线 C ⭐：调云端 `TimeSyncAPI::request`

**做法**：用设备已有的官方 API 从云端取时间。

```
POST/GET https://api2.mina.mi.com/ping/time
（messaging.conf 的 timesync_url）
```

实现路径（两种，都不需要自己实现协议）：

1. **`ma_api_call` C 桩**：`liblibma.so` 是 C ABI（77 个 `ma_*` 函数），
   设备上 `dlopen` 写 ~50 行 C 即可用**官方实现**调；
2. **`matool` 壳**：`libxiaomi_mico.so` 的 `TimeSyncAPI::request`
   已有用户态入口，查 `matool_*` 家族是否有对应子命令。

| 维度 | 评估 |
|---|---|
| 有效性 | ✅ 高（云端权威时间源） |
| 是否写 `/data` | ✅ **否**（一次性请求，可只更新内存时钟 + `touch /tmp/ntp.status`） |
| 是否改 `init.sh` | ✅ **否** |
| 与你铁律的冲突 | ⚠️ 需授权（发起一次云端请求），但**不落盘、不改配置** |
| 额外收益 | 顺带验证了 N5「云端 API 可 dlopen 直调」的可行性，为后续 TTS 直取/点歌铺路 |

---

## 3. 推荐：路线 C

**核心理由**：它是唯一**不写 `/data`、不改 `init.sh`** 的方案。

> ⚠️ **2026-10-20 修正（两点）**：
> ① **`ping/time` 已真机实测：完全无鉴权**（`GET https://api2.mina.mi.com/ping/time` 直接返
> `{"data":{"t":<ms>}}`，RTT 124–170 ms）⇒ 本文件 §5 的 **CLK-3 已闭合**，不必处理认证头。
> ② 「C 替代 A」不成立，实际是**两步互补**：`timefix.sh` 的**明文 HTTP `Date:` 头**是
> 打破 TLS 死锁的**第一步**（HTTPS 取时本身就要求时钟正确），`ping/time` 是**第二步精校**。
> ⇒ 若最终决定部署 A，应在 `timefix.sh` 末尾追加一次 `ping/time`；只做 C 则需先能建立 TLS。
> 现行方案见 [`README.md`](README.md) B-10 / §3.8。

你此前的铁律是「不修改音箱设置/状态」，而路线 A 的两处写入
（`/data/init.sh` hook + `/data/timefix.log`）**正是被否决过的同类操作**。
路线 C 只发起一次云端请求并更新内存时钟，**不触碰任何持久化配置**。

### 3.1 实施顺序

```
① 先确认 matool 是否有现成的 timesync 子命令（零成本，只读）
     ls usr/bin/matool_* | grep -i time
② 若无，写最小 C 桩 dlopen liblibma.so 调 ma_api_call
③ 实测一次（需授权）：确认时钟被修正 + /tmp/ntp.status 出现
④ 确认 mico_aivs_lab 解除等待（日志出现 "time sync & sys config all done"）
⑤ 若需开机自动执行，再评估「是否值得为它加 init.sh hook」（这一步才需要二次授权）
```

### 3.2 授权边界（请明确）

| 动作 | 是否需授权 |
|---|---|
| ① 查 `matool_*` 子命令 | 只读，无需 |
| 写 C 桩放 `~/temp` | 无需（不碰设备） |
| **③ 真机跑一次取时间** | **需授权**（发起云端请求 + 改内存时钟） |
| `touch /tmp/ntp.status` | **需授权**（虽在 tmpfs，但会改变服务行为） |
| ⑤ 加 `init.sh` hook | **需二次授权**（写 `/data`） |

⇒ **最小授权集 = ③ + `touch`**，即可验证整条链路。

---

## 4. 与公钥回归的关系（重要）

`/data/init.sh` 的 hook 机制**已经出过一次事故**：
`cp -f` 覆盖导致 `chen@beijing` 公钥每次重启丢失
（见 [`../../docs/plan/lx06-ssh-key-injection-deviations.md`](../../../docs/plan/lx06-ssh-key-injection-deviations.md)）。

⇒ **这也是「优先选路线 C」的理由**：每往 `init.sh` 里加一段 hook，
就多一个下次重启时静默生效的改动面。**能不加就不加。**

---

## 5. 未解决

| ID | 问题 |
|---|---|
| CLK-1 | `matool_*` 家族是否有现成 timesync 子命令（只读可查；**已不阻塞** —— 可直接 GET `ping/time`） |
| CLK-2 | `ma_api_call` 结构体完整布局（已知 `+8` = api 名；写 C 桩的最后一步） |
| ~~CLK-3~~ | ✅ **已闭合**：`ping/time` **完全无鉴权**，GET 即返回毫秒时间戳（`run/cloud-side-trios-verification.md`） |
| CLK-4 | `touch /tmp/ntp.status` 但时钟仍错误时，下游会怎样（证书校验失败？） |
