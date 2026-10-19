# ASR 输入链可靠性：两个已确认缺陷与 bridge 侧可做的加固

> 状态：**建议文档（未施工）**。本文只提出方案，不改任何代码。
> 取证：`D:\_work\dev\reverse\lx06-firmware-reverse\run\client-monitor-reverse.md`
> 上游源码：`coderzc/open-xiaoai` `packages/client-rust`（commit `14e1034`）
> 日期：2026-10-19

---

## 0. TL;DR

`xiaoai_asr` 是当前主力模式（NAS 双核跑不动本地 ASR），它有**两个输入**，
两者都有已确认的缺陷，但**都属于 client 端，按用户决定不改 client**。

| 输入 | 通路 | 缺陷 | bridge 能否单侧加固 |
|---|---|---|---|
| **ASR 文本** | client 轮询 `/tmp/mico_aivs_lab/instruction.log`（10ms）→ WS | monitor 启动时 `position = metadata.len()` ⇒ **重启即丢该窗口的 ASR**（必现） | ⚠ 部分（见 §2） |
| **麦克风音频** | client `arecord` 子进程 → WS | 读超时/异常时 `_ => break` ⇒ **采集静默永久停止** | ✅ 多数（见 §3） |

**结论**：bridge 侧能做的加固集中在 **§3（音频）** 与 **§2.2（可观测性）**。
两者都不需要改 client。

---

## 1. 事实基础（必须先对齐，否则方案会错）

### 1.1 LX06 是 **6 麦阵列**，不是 8

真机实测（`arecord -D noop -f S32_LE -r 48000 -c 8 -d 1`，逐通道 RMS）：

| 通道 | RMS | 峰值电平 | 判定 |
|---|---|---|---|
| ch0 | 793,435 | −59.0 dBFS | 有信号（底噪） |
| ch1 | 903,873 | −58.3 dBFS | 有信号 |
| ch2 | 1,051,865 | −56.8 dBFS | 有信号 |
| ch3 | 838,971 | −58.1 dBFS | 有信号 |
| ch4 | 1,033,592 | −54.9 dBFS | 有信号 |
| ch5 | 1,132,776 | −57.4 dBFS | 有信号 |
| **ch6** | **0** | **−∞** | **恒零**（无麦克风） |
| **ch7** | **0** | **−∞** | **恒零**（无麦克风） |

⇒ **硬件是 6 麦，`hw:0,3` 以 8ch 格式暴露，最后两路是填充零。**
（此前文档里写「8ch 阵列」是错的，以本文为准。）

**工程含义**：
- 不要对 ch6/ch7 做任何处理（VAD/降噪/取最大通道都会浪费算力或引入误判）；
- 6 路信号的底噪水平接近（−59 ~ −55 dBFS），说明**没有哪一路明显更“主”**，
  即环形阵列的等权特性 ⇒ 简单「取单通道最优」大概率不如 beamforming。

### 1.2 client 的默认采集参数

```rust
// packages/client-rust/src/services/audio/config.rs
AudioConfig { pcm: "noop", channels: 1, bits_per_sample: 16,
              sample_rate: 16000, period_size: 160, buffer_size: 480 }
```

⇒ **client 默认取 1 声道 16 kHz**，靠 ALSA `plug` 把 8ch/48k/S32 下混成 1ch/16k/S16。

### 1.3 A113 的位深转换

```rust
// record.rs::convert_a113_s32_to_s16
// A113 PDM data lives in lower 24 bits of S32_LE: shift by 8 (not 16).
let mapped = (sample >> 8).clamp(i16::MIN as i32, i16::MAX as i32) as i16;
```

⇒ `S32 → S16` 是**硬截断（丢低 8 位）**，无抖动。

---

## 2. 缺陷 A：monitor 启动丢历史（ASR 文本）

### 2.1 机制（源码级）

```rust
// packages/client-rust/src/services/monitor/file.rs
let mut position = metadata.len();          // ★ 启动跳到末尾
loop {
    if current_size < position { position = 0; }   // 只处理"变小"
    ...
    sleep(Duration::from_millis(10)).await;
}
```

配合 `bin/client.rs:87`：

```rust
self.instruction_monitor.start(|event| async move { ... }).await;
// FileMonitor::start 内部：if let Some(old) = self.task_holder.replace(new) { old.abort() }
```

⇒ **每次调用 `start()` 都会 abort 旧 task 并新建，而新 task 从文件末尾开始**
⇒ **每次 WS 重连都丢掉那段时间内的 ASR 结果**。

### 2.2 bridge 侧能做的（**不改 client**）

| # | 加固 | 效果 | 成本 |
|---|---|---|---|
| **A1** | **减少 monitor 重启次数**：WS 断线重连时，若 bridge 侧能保持连接稳定（心跳/重连退避），就能少触发 client 的 `start()` | 直接减少丢事件窗口 | 低（bridge 已连，属配置优化） |
| **A2** | **重连后显式标记**：bridge 在 WS 重连成功时打一条 `INFO` 日志「monitor 已重启，此窗口内的 ASR 可能丢失」 | 把"神秘丢结果"变成"已知窗口"，避免误判为后端故障 | 极低 |
| **A3** | 重连后向用户播一句轻提示（可选） | 用户知道要重说 | 低（但可能扰人，默认关） |
| **A4** | 记录每次 WS 重连的**时间戳**，排障时与用户报告的"没听见"时刻对齐 | 归因效率 | 极低 |

**做不到的**：历史已被 client 跳过，bridge **无法补拉**（`instruction.log` 只留最近一次识别，
且 client 不会回放）。⇒ 只能缓解与标记，不能根治。

> ⚠ **不建议**改成 `position = 0`：那会重放全部历史 ASR ⇒ 可能重复执行旧指令。
> 当前「跳历史」是更安全的取舍。

---

## 3. 缺陷 B：音频采集静默停止（麦克风通路）

### 3.1 机制（源码级）

```rust
// packages/client-rust/src/services/audio/record.rs
loop {
    match timeout(Duration::from_millis(500), stdout.read(&mut buffer)).await {
        Ok(Ok(size)) if size > 0 => { /* 累积并发给 WS */ }
        _ => break,                     // ★ 任何异常/超时都直接退出
    }
}
let _ = AudioRecorder::instance().stop_recording().await;   // 然后永久停止
```

`_ => break` 覆盖了三种情况：**读超时（500ms 无数据）**、**读错误**、**EOF**。
其中「读超时」在设备负载高时（同时播放 TTS / 音频输出）**完全可能发生**。

⇒ 一旦触发：**采集停止，但 WS 连接仍在，bridge 侧看起来一切正常**——
音频流只是不再来了。这是**最难排查的一类故障**。

### 3.2 bridge 侧能做的（**不改 client**）

#### B1（**最高优先级**）：音频静默检测器

`xiaoai_asr` 模式下，bridge 应该能回答「麦克风还在出声吗」。

```
判定：最近 N 秒内是否收到过音频帧？
  - 收到 → 正常
  - 超时未收到 → 告警（日志 + 管理面板状态 + 可选 HA 事件）
```

**判据设计**（关键，避免误报）：

| 参数 | 建议值 | 理由 |
|---|---|---|
| 无帧告警阈值 | **3 秒** | 正常时 client 每 `buffer_size/rate` = 480/16000 = **30ms** 发一帧；3 秒 = 100 帧的空窗，远超抖动 |
| 连续静音（全零/极低能量）告警 | **30 秒** | 用户不说话也会有空帧，不能太敏感 |
| 恢复判定 | 收到 1 帧即恢复 | — |

**为什么这值得做**：这是**唯一能在 bridge 侧发现「client 采集已死」的手段**。
没有它，故障表现为「喊了没反应」，而你无法区分是 KWS 没触发、ASR 没识别、还是音频根本没上来。

#### B2：重连自愈

检测到音频静默后：
1. 主动发 `stop_recording` / `start_recording` RPC（client 暴露了这两个命令，
   见 `bin/client.rs` 的 `rpc.add_command("start_recording", …)`）；
2. 若重试 N 次仍无音频 → 降级告警「需要重启 client」。

⚠ **前提**：client 的 `start_recording` 在 `State::Recording` 时直接 `return Ok(())`（幂等短路），
所以**必须先 `stop_recording` 再 `start`**，否则是空操作。

#### B3：`arecord` 设备冲突检测

client 用 `arecord -D <config.pcm>` 抓麦克风（默认 `noop`）。
若 bridge 侧也要 `arecord -D noop`（8ch 采集做 AEC/远场），**两者会抢设备**。

| 场景 | 现状 | 建议 |
|---|---|---|
| 只用 `xiaoai_asr` | ✅ 无冲突（bridge 不抓音频） | — |
| 想同时做 8ch 分析 | ❌ 冲突 | 串行化，或改用 client 上报的音频流 |

⇒ **side note**：这解释了 `hw:0,3` → `noop` patch 的另一面——
patch 让 `noop` 变成「可并发打开」的软设备（`dsnoop` + `plug`），
所以 1ch 与 8ch **能同时抓**（实测 rc=0 两者都成功）。
但**两个 client 级的 arecord 同时跑仍会互抢**。

#### B4：`arecord` 输出的 WAVE 头陷阱

实测：`arecord -D noop -f S16_LE -r 16000 -c 1 -d 1 /tmp/t1.raw` 产出 **32,044 字节**
（预期 32,000）⇒ **多出 44 字节 WAVE 头**，因为没加 `-t raw`。

client 源码里加了 `-t raw`，所以**当前无问题**。
但 bridge 侧若自己调 `arecord` 抓数据，**必须加 `-t raw`**，否则每段开头会有 44 字节垃圾，
把 ASR 的 PCM 流打歪。这是一个**低成本高杀伤**的坑。

---

## 4. 建议的施工顺序

| 步骤 | 内容 | 依赖 | 风险 |
|---|---|---|---|
| **1** | **B1 音频静默检测器** | 无（纯 bridge 侧观测） | 极低 |
| 2 | A2 + A4（重连标记与时间戳） | 无 | 极低 |
| 3 | B2 重连自愈（stop→start RPC） | 需 client 暴露 RPC（已有） | 低（有重试上限） |
| 4 | B3/B4 写入 `bridge/AGENTS.md` 的坑清单 | 无 | 无 |
| 5 | 评估 6ch 直采（需改 client，**已排除**） | — | — |

**不建议现在做的**：
- 改 client 的 `channels`（需交叉编译 + 刷设备，且 6 路等权、收益不确定）；
- 加 dithering（`S32→S16` 硬截断）——对 ASR 影响未知，需 A/B 且改动在 client 侧。

---

## 5. 与既有文档的关系

| 文档 | 关系 |
|---|---|
| `../devices/lx06/ir-agent-api.md` | 红外，与本文无关 |
| `../../../docs/plan/home-theater-automation-plan.md` | 取 `xiaoai_asr` 模式的原因（NAS 算力） |
| 逆向侧 `run/client-monitor-reverse.md` | 本文的取证来源（含 `file.rs` / `record.rs` 全文分析） |

---

## 6. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-19 | 首次发布。依据：client 源码（`14e1034`）+ 真机逐通道能量实测（**确认 6 麦、ch6/ch7 恒零**） |
