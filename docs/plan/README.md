# bridge 侧计划权威文档（项目侧）

> **本文是 bridge 仓库内「计划 / 待办 / 授权 / 止损」的唯一权威源。**
> 重写 **2026-10-20 夜**：把 `docs/plan/` 原有 5 篇整合为一份，并**就地修订**其中的过时断言
> （旧版内容仍散落在各文件的历史正文与 `git log` 中，逐步收敛，见 §7 取代链）。
>
> 层的分工（哪类内容该写在哪里）：
>
> | 层 | 位置 | 放什么 |
> |---|---|---|
> | **项目侧计划** | **本文** `bridge/docs/plan/README.md` | bridge 要做什么、顺序、授权、止损 |
> | 设备能力事实 | `docs/devices/lx06/`（工作区根仓） | 设备有什么能力、参数取值域 |
> | 逆向过程与取证 | `D:\_work\dev\reverse\lx06-firmware-reverse\run\` | 怎么解出来的、真机实验记录 |
> | 逆向侧活计划 | `...\run\plan\ROADMAP.md` §3 | 逆向侧还剩什么活（**纯逆向/实机验证** R-01…R-08，不含 bridge 代码改动） |
> | 工作区级框架 | `D:\_work\repos\open-xiaoai\docs\plan\lx06-device-capability-plan.md` | 跨子系统（HA / 红外 / 音乐 / bridge）的接入排序与历史决策，**不作 bridge 施工入口** |
>
> **排序原则**：§2 按「潜在收益」降序 —— 收益 = 对现网可靠性的影响 × 用户可感知增量 ÷（成本 × 前置阻塞）。

---

## 0. 现状一句话

**bridge 只调 4 个 ubus 方法**（取数：`cd open-xiaoai && grep -rhoE "ubus call [a-z_]+ [a-z_0-9]+" --include=*.py bridge/ | sort -u | wc -l`），
设备已解出 258 个 ⇒ 利用率 ≈1.6%。但**当前瓶颈不是「接得少」，而是已接的三条链路各有一个确定性缺陷**：

| 已接链路 | 缺陷 | 影响 |
|---|---|---|
| TTS 播报（`tts_play.sh`） | 退出码成功也返 1，代码用 `exit_code == 0` 判成败 | 阻塞播报**恒返 `False`**，上层 fallback 被误触发 |
| 播报期间防自触发 | 只有软件闸门 `_gate_kws`，无固件级闭麦 | 已两次生产事故（KWS 自触发回环） |
| TTS 文本转义 | 只做 shell 转义，未做 JSON 转义 | 文案含 `"`/`\` 时 ubus **整条拒收**（`rc=255`） |
| 收音 | client 按 1ch 下混，6 麦只用了平均值 | ASR 信噪比白丢 8.33 dB（波束可拿回 +15.56 dB） |

⇒ **本文 §2 的前 4 项全部是「修已有链路的确定性缺陷」，不是「加新能力」。**

---

## 1. 事实基线（后续所有方案都依赖这些，改动需同步改本节）

| 项 | 事实 | 取证 |
|---|---|---|
| 麦克风 | **6 麦**（`hw:0,3` 以 8ch 暴露，ch6/ch7 恒零、非零率 0.00%） | `run/n2-six-mic-array-verified.md` §1 |
| 采集入口 | `noop` ≡ `mico_record`（`dsnoop`），可与 `mipns-xiaomi` 并发 | 同上 §4.1 / §4.2 |
| 采集规格 | `-c 6 -f S16_LE -r 16000` 一次到位（ALSA plug 做 48k/S32→16k/S16 + 裁通道），**192 KB/s** | 同上 §4.5 |
| `channels` 来源 | bridge 经 RPC 下发的 `AudioConfig`（client `.rodata 0xbcf1a` 只有 argv 骨架）⇒ **改 bridge 常量即可，client 不用重编** | 同上 §4.5；`plan/ROADMAP.md` §3.2 |
| 闭麦入口 | `pnshelper event_notify {"src":3,"event":7}`（闭）/ `{"src":3,"event":8}`（开）：**立即生效、非阻塞** | `run/pnshelper-src-event-exact-enum.md` |
| 闭麦与采集 | 关麦**不关 ALSA 采集**（8ch 仍可抓）⇒ 可与 bridge 自己的 KWS 并存 | `run/local-control-api.md` §3.3 |
| 开麦与判据 | `event:8` 幂等、`ubus -S` 格式见事实层；`set_mic` 已按回读判定（ticket 03/10） | `ws: docs/devices/lx06/pnshelper-event-space.md` §3.5/§3.6 |
| `alarm micmute` | ❌ **不是闸门**：是闹钟响铃静音流程（回调 `sleep(7)` → `pns mic_off` → 播放 → `pns mic_on`），`{h,m,s}` 是**到期时刻**、调用阻塞约 7 s | `run/local-tts-playback-api.md` §5.2；`run/plan/ROADMAP.md` §3.1 |
| 播放完成事件 | `play_status_broadcast_event`：`status:1` 开始 / `status:0` 结束，**严格成对**（12/12）；**无 id** | `run/play-status-event-full-lifecycle.md` |
| 事件发送者 | ❌ **不是** `mode_manager`（该名字不是 ubus 对象） | `run/device-local-http-and-event-source-correction.md` §2 |
| 音量量纲 | 档位 0–100（`player_set_volume`，无钳位）/ 百分比（`player_set_continuous_volume` 0–100）/ `get_media_volume` 非线性 25–255 / `amixer` 恒 85% 不随动 | `run/volume-caliber-mismatch.md` |
| `tts_play.sh` rc | **成功也返 1**（尾行 `[ ... ] && {...}` 短路）；另有 `my_log` 未定义 | `run/tts-e1-runtime-verification.md` |
| TTS 直取 | `text_to_speech {save:1,play:0}` 零凭证落盘 `/tmp/tts/*.mp3`；`file://` 播放会触发完成事件 | `run/tts-audio-direct-fetch.md` |
| 设备内存 | 118 MB、无 swap，常态 free 2.8–5.9 MB；`mediaplayer` 崩后 **procd `retry=0` = 无限重启（会自愈）** | `run/mediaplayer-crash-no-respawn.md` |
| 设备校时 | `https://api2.mina.mi.com/ping/time` **完全无鉴权**，GET 返回 `{"data":{"t":<ms>}}`，RTT 124–170 ms | `run/cloud-side-trios-verification.md` |

🔴 **禁区（绝对不调）**：`miio` 全对象、`messagingagent identify_device/set_kv`、`service` 写组、`system reboot/sysupgrade/watchdog`、
`mediaplayer player_event`、`notify` 整对象、DLNA 开启。完整清单见 `run/plan/ROADMAP.md` §4.2。

---

## 2. 活待办（唯一清单，按潜在收益降序）

> 规则：上限 10 项，超了先合并/降级；**新增结论先写进 §3.x 的方案节，再回写本表**。
> 「前置」是硬阻塞；「授权」指动设备状态或需用户拍板的动作。

| ID | 待办 | 收益 | 成本 | 前置 / 授权 | 状态 |
|---|---|---|---|---|---|
| **B-01** | **阻塞播报改用副作用判成败**（弃 `exit_code`） | ⭐⭐⭐ 恢复播报成败判定，止住误触发 fallback | 极低（1 处判据） | 无 | ✅ **已施工（ticket 03，2026-10-24）** |
| **B-02** | **播报期间固件级闭麦**（`pnshelper` 7/8 双端夹逼）+ **同批修 `set_mic` 语义反转** | ⭐⭐⭐ 根治已两次发生的 KWS 自触发回环 | 低 | 需先拍板 T7.6 期望（§3.2） | ✅ **已施工（ticket 03 修语义 + ticket 10 固件闭麦闸门，2026-10-24）** |
| **B-03** | **六麦采集接进 bridge**（`channels: 6` + `reshape`） | ⭐⭐⭐ ASR 信噪比 +8.3~15.6 dB | 低 | 无（先做 §3.3 的帧长观测） | ✅ **已施工（ticket 04，2026-10-24）** |
| **B-04** | **TTS 文本 JSON 转义**（`speaker.py:75`） | ⭐⭐ 消除「文案含引号就哑」的潜伏故障 | 极低（10 行） | 无 | ✅ **已施工（ticket 03，2026-10-24）** |
| **B-05** | **播放完成事件替代时长估算** | ⭐⭐ 闸门精确化，去掉盲等 | 低 | 无 | ✅ **已施工（ticket 05，2026-10-24；原生源需额外部署通道，默认 context + client 影子）** |
| **B-06** | **音频静默检测 + 重连自愈** | ⭐⭐ 唯一能在 bridge 侧发现「client 采集已死」的手段 | 低 | 无 | ✅ **已施工（ticket 04 看门狗；client 重连由 client 自身）** |
| **B-07** | **mediaplayer 存活探针 + 内存红线** | ⭐⭐ 内存临界反复崩时能快速发现 | 低 | 无 | ✅ **已施工（ticket 03 探针 + ticket 08 健康聚合，2026-10-24）** |
| **B-08** | **音量通道收口**（设用百分比、读用 `player_get_context`） | ⭐⭐ 去掉自然语言绕路 + 修量纲 | 低 | 无 | ✅ **已施工（ticket 03，2026-10-24）** |
| **B-09** | **`InjectRelay` 文本回放**（`seq` + `?since=` 断点续取） | ⭐⭐ 修「推流成功但没人收到」 | 低 | 无 | ✅ **已施工（2026-10-21）** |
| **B-10** | **时钟死锁干净解**（`ping/time`） | ⭐ 每次断电复发的断网隐患 | 低 | 无（2026-10-25 用户授权） | ✅ **已施工（init.sh 后台段，仅 ntp.status 缺失时生效；顺带修复 init.sh exec 阻塞 bug——原 exec client 导致其后所有持久化段永不执行，2026-10-25 reboot 实测发现并修复）** |

> 本轮 baseline 的 ticket 01–10 与逐项落点见 `ws: .scratch/lx06-voice-hub-baseline/`；本表状态与其同步。
> 2026-10-24 已部署到 NAS：镜像 `open-xiaoai-bridge:04b0709e51`；回滚 tag `open-xiaoai-bridge:rollback-20261024`。
> 后续验证/优化（ASR A/B、注入 E2E、native 事件源、架构 preflight）见
> `ws: .scratch/lx06-voice-hub-followups/`；B 列表只保留 bridge 施工。

**已降级为台账（不再列为待办）**：

- **`chen@beijing` 公钥覆盖回归**：root 口令是公开默认值 ⇒ 该回归**不扩大攻击面**，只收窄本人合法访问路径；
  与「设备仅内网使用」的基线一致，不投入成本（事实见 `docs/plan/lx06-ssh-key-injection-deviations.md`）。
  原 `pending-decisions.md` 第 5 项在此结案。
- **`api_server.py` 工作区改动去留**：并入 B-09，不再单列（原 `pending-decisions.md` 第 6 项）。
- **README 索引类维护**：不占待办额度。

---

## 3. 方案节（每项的判据与落点）

### 3.1 B-01 阻塞播报的成败判定

```python
# core/services/speaker.py:75-78
command = f"/usr/sbin/tts_play.sh '...'"
res = await self.run_shell(command, timeout=timeout)
return res.exit_code == 0        # ← 恒 False
```

**为什么恒 False**：`tts_play.sh` 最后一条命令是 `[ x"$player_stat" == x"1" ] && { mphelper play }`；
原本不在播时 `test` 返回 1，在播时返回 `mphelper play` 的码 —— **两种情况都不保证 0**。

**改法（按成本递增，选一）**：
1. **副作用回读**（推荐，零依赖）：播完后 `test -f <path>`（需 `save:1`），或读 `player_get_play_status`（`0`=空闲）；
2. **事件驱动**（依赖 B-05）：等 `play_status_broadcast_event` 的 `status:0`，超时兜底；
3. 不改 `tts_play.sh`（设备侧脚本，属禁区外的只读资产，不动）。

**验收**：一次正常播报返回 `True`；一次故意失败的播报（如断网时 `text_to_speech` 返 `code != 0`）返回 `False`。

### 3.2 B-02 播报期间闭麦（含 `set_mic` 语义修复）

**接口**（已真机 VERIFIED，非阻塞、立即生效）：

```bash
ubus -t1 -S call pnshelper event_notify '{"src":3,"event":7}'   # 闭麦：creat /tmp/mipns/mute (0600)
ubus -t1 -S call pnshelper event_notify '{"src":3,"event":8}'   # 开麦：unlink /tmp/mipns/mute
```

**施工形态**（双端夹逼，不留猜测窗口）：

```python
await speaker.set_mic(False)      # 语义修正后 = 闭麦
try:
    await speaker.play(...)
finally:
    await speaker.set_mic(True)   # 必须 finally，否则麦永久关闭
```

**🔴 必须先修的语义反转**：`speaker.py:206-217`（`set_mic`）里 `on=True` 分支（`speaker.py:210`）发 `event:7`（=**闭麦**），
`else` 分支（`speaker.py:214`）发 `event:8`（=**开麦**）；而 `get_mic()`（`speaker.py:198`）读同一文件、逻辑正确 ⇒ 二者互相矛盾。

**必须先拍板的一件事（T7.6 的真实期望）**：`deploy/config.py:204-211` 的「停止聆听」用
`set_mic(False)` + `kws.disable_listening()`，注释写「硬件麦克风静音 + KWS 持久停止分析」。

| 期望 | 现在实际发生 | 该怎么做 |
|---|---|---|
| **只要 KWS 停**（隐私靠软件闸门） | KWS 确实停了（`disable_listening`），设备级静音**从未生效** | 改掉错误注释与日志，`set_mic` 要么删除、要么改成显式正确的调用 |
| **要设备级静音**（硬件层也停唤醒） | 没做到 | 按上面的双端夹逼实现，并修正 `api_server.py:734-735` 那段**因果写反的注释** |

**与软件闸门的关系**：`_gate_kws`（bridge 不喂帧）与固件闸门（`/tmp/mipns/mute`，固件不唤醒）**叠加**，
不互相替代：前者防 bridge 自身 KWS 自触发，后者防固件唤醒引擎被播报声触发。
前提是「关麦不影响 ALSA 采集」（§1 已验）。

**验收**：① 播报期间 `get_mic()` 返 `off`，播完 1 s 内返 `on`；② 播报文案故意含 KWS 词表条目，连播 3 次命中 **0** 次
（A/B：关掉本闸门应能复现命中）；③ 播报抛异常后麦仍为 `on`。

### 3.3 B-03 六麦采集

**改两处 + 一处解析**（`native/src/lib.rs:24` 是**播放**配置，**勿动**）：

| 位置 | 改动 |
|---|---|
| `native/src/lib.rs:122`（`start_recording` 的 `AudioConfig`，第 118–127 行块） | `channels` 1 → **6** |
| `native/src/server.rs:43-48` | `test()` 里的同款配置 1 → **6** |
| `core/xiaoai.py:135` | `np.frombuffer(dtype=int16)` 后 **`reshape(-1, 6)`** 再做选麦/波束降到 1ch |

**施工第一步只做观测（勿跳）**：`buffer_size=1440`/`period_size=360` 是按 1ch 计的样本数，
`channels: 6` 后 client 的组帧口径有两种可能（每声道 1440 样本 ⇒ 17,280 B；或总量不变 ⇒ 2,880 B），
**两者都能被 6×2=12 整除，但帧长不同** ⇒ 先在 `on_input_data` 打一次 `len(data)` 与 `len(data) % 12`，
据此定 `reshape` 与是否需要残留缓冲。若 `len % 12 != 0`，说明存在跨帧残留，必须加缓冲。

**降级路径**（不动 bridge 常量化）：容器内自取
`ssh root@lx06 arecord -D noop -f S16_LE -r 16000 -c 6 -t raw -`（实测 3 s 得 576,000 B = 192 KB/s）。
代价是多一条 SSH 长连接，且与 client 的 `arecord` 是两个独立进程（`dsnoop` 允许并发，各有缓冲）。

**验收**：改前后同一段语音的 ASR 结果对比（同一房间同一距离，各 10 次，比较识别成功率）；
另需一次 `reshape` 正确性检查（6 路信号应各不相同，不应出现某一路恰为另一路的移位副本）。

### 3.4 B-04 TTS 文本 JSON 转义

`core/services/speaker.py:75` 只做 shell 单引号转义，而设备侧 `tts_play.sh:5` 把 `$1` **直接插进 JSON 字面量**
⇒ 文本含 `"` 或 `\` 时 JSON 破裂。

**真机实测已定案**：不是「静默截断」，而是 ubus **整条拒收**（`rc=255` + `Failed to parse message data`），
不生成音频、请求根本没到 `mibrain`（`run/tts-e1-runtime-verification.md`）。

**修法（顺序不能反：先 JSON，后 shell）**：

```python
_safe = (_raw.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
_safe = _safe.replace("'", "'\\''")
command = f"/usr/sbin/tts_play.sh '{_safe}'"
```

**更稳的替代**：绕开双层转义 —— 用已安全的 `json_encode` 走 `text_to_speech {save:1}` 取 `path`，
再 `miplayer -f <path>`（代价：改变 blocking 语义 + 需清理临时文件）。

**同类排查结论**：全仓 4 处 shell 套 JSON 中**只有 `tts_play.sh` 这条有洞**，其余 3 处都走 `json_encode`。

### 3.5 B-05 播放完成事件

```
<- <conn> invoke: {"objid":1,"method":"send","data":{"id":"play_status_broadcast_event","data":{"status":1}}}
-> <sub>  invoke: {… "method":"play_status_broadcast_event","data":{"status":1}}   ← 逐次成对
```

落点**三处**（不是一处）：`api_server.py:244 _estimate_gate_seconds` 降级为**超时兜底上限**；
`:417` 的 `asyncio.sleep(估算值)`、`:483` 的 `max_seconds=120.0` + `:489` 的 `asyncio.sleep(120.0)`、
`:574` 的 `max_seconds=300.0` 改由事件驱动提前解锁。

**三条硬边界**：① 事件**无 id** ⇒ 仅串行可用；② `status:0` = 链路空闲（失败也翻 0）；③ HTTP 源不可达时**只发 `status:1`** ⇒ 必须保留超时兜底。

### 3.6 B-06 / B-09 两条「丢事件」缺陷（不同侧，同一症状）

| 侧 | 根因 | 现状 |
|---|---|---|
| client | monitor 启动时 `position = metadata.len()` ⇒ 重启即丢该窗口的 ASR（源码级确认） | bridge **无法补拉**，只能减少重启次数 + 重连时打标记 |
| **bridge** | `InjectRelay.publish_text()` 只做实时广播，而订阅队列是 **WS 连接时**才创建 ⇒ 订阅晚于 ASR 结果即永久丢失 | ✅ **已修（2026-10-21）**，见下 |

**最终实现（2026-10-21，已提交）——不是 `text_buffer`，而是 `seq` + `?since=`**：

初版方案（窗口期 `text_buffer` + 重放）有**两个**问题，均已弃用：

1. **顺序陷阱**：`create_task` 建在重放之前 ⇒ `await send_json` 让出控制权 ⇒ 实时可能插进重放中途 ⇒ 乱序。
2. **清空时机错误**：`open_session()` 里 `text_buffer.clear()` ⇒ PC 在窗口结束**后**才连 WS（正常操作：松开快捷键后工具才连）时，文本已被清掉。
   **这与它要修的「晚连」是同类问题，只是把时间窗从几十毫秒拉长到一个会话。**

现方案：

| 机制 | 作用 |
|---|---|
| `_seq` 单调递增、**永不复位** | 客户端可去重 / 排序 / 断点续取 |
| `_text_log` **有界裁剪**（`MAX_TEXT_LOG=200`） | 内存有上界（约数十 KB），且**不随窗口清空** |
| `GET /api/inject/text?since=N` | 只回放 `seq > N`，断点续取；**缺省 `since` 只回放最近一次开窗之后**（显式 `since=0` 才回放全部保留历史），避免新窗口把上一轮文本重放给 PC |
| 每条帧带 `seq` | 实时/回放用同一序号空间，**不再依赖时序运气** |
| **先回放、后订阅实时队列** | 结构性消除顺序陷阱（非靠调用顺序约束） |

✅ **消费端已适配新协议**：`scripts/pc-inject/pc_inject.py`（workspace 层，不在 bridge 子仓）
已使用 `is_final` 判「说完」、`seq` 去重、`?since=` 断点续取（commit `f24e033`，2026-10-21）；
不再依赖「累计字数 / 3 秒静默」启发式（旧 bridge 无 `is_final` 时保留兼容兜底）。

（aurora 是 NAS 上的对话后端，与注入链路正交，**不订阅此 WS**。）

**2026-10-21 真机实测补充（`is_final`）**：对一句话，云端会发**多条** `RecognizeResult`，
且 `text` 是**累积快照**（「今天天气怎么样」实测 **8 条**：`今`→`今天天气`→…）。
`is_final=true` **只在最后一条**；第 7 条（`is_nlp_request`+`is_stop`）与第 8 条**文本相同**。

⇒ 帧格式扩为 `{"event":"text","seq":N,"text":"...","is_final":bool}`：
PC 端按 `is_final` 区分「预览」与「提交」，且必须**覆盖**而非追加（因为是快照）。
中间快照**不过滤**——语音输入法需要边说话边出字。

设备事实：[`docs/devices/lx06/recognize-result-event-sequence.md`](../../../docs/devices/lx06/recognize-result-event-sequence.md)

**端到端延迟实测（2026-10-21）**：设备自报埋点 `asr_send_end_2_nlp` = **51 ms**，
设备侧共 **62 ms**，端到端 **≈90–175 ms（典型 120）** —— 比原估 0.3–0.8 s **快 2–6 倍**。
⚠️ 两次实测该段 **51 / 529 ms**（10 倍波动，含云端排队）⇒ **不可按 51 ms 做 SLA，按 ~600 ms 做超时兜底**。
额度：**设备侧不可见**（`aivs_config` 无 quota 字段），只有 `TOO_MANY_*` 错误码 + `hijack_detect` 探针；
`realSingleLossy=100` 说明设备自己在降采样省额度。
数据 → [`docs/devices/lx06/voice-input-latency-and-quota.md`](../../../docs/devices/lx06/voice-input-latency-and-quota.md)

测试：`tests/test_inject_text_replay.py`（17 条，纯内存 asyncio，不出声不碰设备）。

> **已修复（ticket 03/10，2026-10-24）**：`set_mic` 已改为按 `get_mic()` 回读判定；
> `event:8` 幂等与 `ubus -S` 输出格式的权威事实见
> `ws: docs/devices/lx06/pnshelper-event-space.md` §3.5/§3.6。
> 播报固件闭麦与真机验收结果见
> `ws: .scratch/lx06-voice-hub-baseline/issues/10-playback-mic-gate.md` 与 `09-device-acceptance.md`。

**B-06 的判据设计**（音频静默检测，避免误报）：正常 30 ms 一帧；**无帧告警 3 s**（=100 帧空窗）；
**连续静音告警 30 s**（用户不说话也有空帧）；收到 1 帧即恢复。恢复动作 = `stop_recording` → `start_recording`
（client 的 `start_recording` 在 `State::Recording` 时幂等短路，**必须先 stop**）。

**排障前置判据**：做任何「修 ASR / 调 KWS」的实验前，先看设备侧 `netstat -tn | grep 4399` 是否 `ESTABLISHED`。
没有它，下游所有现象都无意义（会把网络故障误判成 ASR 缺陷）。

### 3.7 B-07 / B-08 存活与音量

**存活**：`service list {"name":"mediaplayer"}` 返回 `running`/`pid`/`command`/`respawn` —— **比 `ps` 多出
procd respawn 语义**，是权威探针。`respawn.retry=0` = **无限重启**（会自愈）⇒ 缺的是「发现」而非「拉起」；
`ubus call` 返 `code:-1` 是对象消失的特征，应立刻查存活。另轮询 `system info` 的 `memory.free`，
**free < 10 MB 时降级播报并发**。不做手动 `/etc/init.d/mediaplayer start`（procd 已会自愈）。

**音量**：设用 `player_set_continuous_volume {volume:0-100}`（百分比语义明确），
读用 `player_get_context.volume`（**必须回读确认**），**不要**用 `get_media_volume`（非线性 25–255）、
**不要**用 `amixer`（恒 85% 不随动）。另注意 `nightmode` 的非对称调容策略：
**日常模式只允许调大、夜间模式允许调小**，且 `mediaplayer` 判定夜间时**只取 `volume` 字段** ⇒
「设了音量但被纠正」先查这条。bridge 应尊重既有 22:05–07:05 配置，不另设一套。

### 3.8 B-10 时钟死锁

**根因**（`libmico-common.so` 反汇编确证）：`mico_aivs_lab` 启动时等
`access("/tmp/ntp.status")`（tmpfs，**掉电即失**）且 `access("/data/status/config_done")`，
都满足才继续；而冷启动时钟被设为固件构建日（早于云端证书 `notBefore`）⇒ HTTPS 全挂 ⇒ 拿不到校时
⇒ **永久自旋**。**每次断电复发。**

| 路线 | 是否写 `/data` | 是否改 `init.sh` | 评价 |
|---|---|---|---|
| A `timefix.sh`（已有，未部署） | 🔴 是（`init.sh` hook + 日志） | 🔴 是 | 同类操作此前被否决过；每加一段 hook 就多一个静默生效的改动面 |
| B 改 `/etc/init.d/boot` | — | 改只读 squashfs | 需重打包固件，不划算 |
| **C `ping/time`** ⭐ | ✅ 否 | ✅ 否 | **已实测无鉴权**（§1）；只需一次 GET |

**推荐 C，但顺序是「两步」而非「替代」**：`timefix.sh` 的「明文 HTTP `Date:` 头」是
**打破 TLS 死锁的第一步**（HTTPS 取时本身就要求时钟正确），`ping/time` 是**第二步精校**。
⇒ 实际做法：`timefix.sh` 末尾追加一次 `ping/time`（若最终决定部署 A），或仅用 C 时需说明「首次仍需能建立 TLS」。
**授权边界**：① 查 `matool_*` 子命令 = 只读免授权；② 真机跑一次取时间 + `touch /tmp/ntp.status` = **需授权**；
③ 加 `init.sh` hook = **需二次授权**。

**未解项**：`ma_api_call` 结构体完整布局（已知 `+8` = api 名）—— 若要写 C 桩，这是最后一步；
若走 `curl`/`matool` 壳则不需要。

---

## 4. 施工排序（按收益 × 风险，与 §2 同序，这里给批次）

| 批次 | 内容 | 为什么这一批 | 风险 |
|---|---|---|---|
| **第 1 批（零授权、零设备改动）** | B-01 · B-04 · B-07 · B-08 | 全是判据/量纲修正，改完可立即用日志验证 | 极低 |
| **第 2 批（零授权、需观测先行）** | B-03（先打帧长日志）· B-05 · B-06 | 都要先拿到一次真实观测样本再动逻辑 | 低 |
| **第 3 批（需拍板）** | B-02（先定 T7.6 期望） | 涉及隐私语义 | 低~中 |
| **第 4 批（需授权）** | B-10 | 动设备时钟 | 低 |

**跨批红线**：
- 播报必须**串行**（设备 118 MB 无 swap，并发在内存临界时是明确触发因素）；
- 任何 `ubus call` **短超时（≤6 s）+ 异步**，绝不阻塞主流程；阻塞型方法清单见 `run/plan/ROADMAP.md` §4.5；
- 真机测试：低频（sleep ≥1 s）、单路径、批量前 `find /data /etc -newermt` 快照、free<10 MB 停手；
- 发现设备**无日志输出** = 整机冻结 ⇒ 立即断电上电，不要试 ubus/reboot。

---

## 5. 止损记录（方向已判定无效，勿重复）

> 本节只增不删。

| 方向 | 结论 |
|---|---|
| 用 `alarm micmute` 做播报闸门 | ❌ 它是闹钟响铃静音流程（回调先 `sleep(7)`），`{h,m,s}` 是到期时刻 ⇒ 不再投入定标成本，改用 `pnshelper event 7/8`（§1、§3.2） |
| 改 `channels` 需重编 client | ❌ `channels` 是 RPC 下发参数 ⇒ 改 bridge 常量即可（§1） |
| 用 `exit_code` 判播报成败 | ❌ `tts_play.sh` 成功也返 1（§3.1） |
| 靠「等设备自愈」恢复 `messaging.cfg` | ❌ `channel_secret` 不在任何自动刷新范围；恢复必须走 `device_token/v2/transfer` 或米家 App 重绑 |
| 把 `identify_device` 当只读调用 | ❌ 已造成真机事故（覆盖写 `messaging.cfg`）。判据 = 反编译主函数体 + 副作用回读 |
| 非 root 调 ubus | ❌ `/usr/share/acl.d` 不存在 ⇒ 对 `uid!=0` 一律 `PERMISSION_DENIED`（`miot_rpc` 等仍需 root 会话） |
| 用 `notify` / `mdplay.notify_*` 做播报或订阅 | ❌ `code:0` 假成功 / 进程内自同步，外部无事件可收 |
| 用 `aivs_event_post` 注入任意指令 | ❌ 恒 `code:1`（缺云端 `dialog_id`）；usock 侧只有 4 个硬编码动作白名单 |
| 改 client 的 `S32→S16` 硬截断加 dither | ❌ 对 ASR 影响未知，需 A/B 且改动在 client 侧；收益不确定 |
| 启用 `examples/kws`（设备侧 monitor） | ❌ 当前选的是 bridge 侧 sherpa-onnx；启用前必须先修 `kws.rs:52-53` 的 panic（一行坏数据即永久死亡） |
| 改 `asound.conf` 关 DTS 做 A/B | ❌ `01-asound.patch` 已改过该文件，再改易冲突 |

---

## 6. 已完成 / 无需行动（不要重复列待办）

| 项 | 结论 |
|---|---|
| 红外发射 | ✅ 执行器已回迁 NAS 并端到端验证（内核 `[ir_tx] send finished 68`），安全红线 5 条复验生效。唯一剩余前置 = 码值库（固件内无，需外部提供或 `learn` 录制，**会改设备状态，待授权**） |
| TTS 直取 | ✅ `{save:1,play:0}` 零凭证落盘；在 bridge 真实宿主（NAS）跑通，md5 三重一致。3 条工程约束：`scp` 不可用走 `ssh cat`、私钥须显式 `-i`、dropbear 需 `+ssh-rsa` |
| 播报协同投屏 | ✅ `casting_pause{"from":"tts"}` + `casting_resume` 是原厂标准姿势（无投屏时安全空操作）；**勿用 `casting_toggle`**（无投屏返 `code:1`） |
| 设备健康判据 | ✅ 勿用 `ubus list \| wc -l`（偶发单行输出会被数成 1）；用 `tr + grep -c`（=25），或 `service list` 看 `running` |
| `set_mic` 语义反转的取证 | ✅ 已完成（真机实证 + 反编译），只剩「拍板 + 改代码」→ B-02 |

---

## 7. 取代链（旧文 → 现行权威）

**判据**：被推翻的结论正文已就地改正；此处登记「谁取代了谁」，避免旧文被再次当权威引用。

| 旧文档 / 旧断言 | 现行权威 |
|---|---|
| `pending-decisions.md`（6 项待定索引） | **本文 §2**（1/2/6 → B-09、B-04；3 → B-02；4 → B-10；5 → §2 台账结案） |
| `asr-input-reliability.md`「8ch 阵列」 | ❌ 6 麦（ch6/ch7 恒零）→ §1 |
| `asr-input-reliability.md`「下混损失约 3.8 dB」 | ❌ **8.33 dB peak / 10.54 dB rms** → `run/n2-six-mic-array-verified.md` §3 |
| `asr-input-reliability.md`「非对角相关 0.573」 | ❌ **0.111**（声场不同，独立性更强）→ 同上 §2 |
| `asr-input-reliability.md`「6ch 直采需改 client，已排除」 | ❌ `channels` 由 bridge 下发 ⇒ 不需改 client → §1、§3.3（B-03） |
| `asr-input-reliability.md`「两个都直接开 `hw:0,3` 会冲突」 | ⚠️ 精确化为：**都走 `noop` 时可并发**（`dsnoop`）→ `run/n2-six-mic-array-verified.md` §4.2 |
| `tts-text-escaping-fix.md`「TTS-E1 报错 or 静默截断，未测」 | ❌ 已真机定案：**只有报错**（`rc=255`，整条拒收，不生成音频）→ `run/tts-e1-runtime-verification.md` |
| `tts-text-escaping-fix.md` §6「TTS-E1/TTS-E2/TTS-E3 未解决」 | E1 已闭合（同上）；E2 已排查（全仓 4 处仅 1 处有洞）；E3 = `save:1` 返回 `info.path` 内层 JSON，清理责任在调用方 |
| `clock-deadlock-resolution.md`「CLK-3 是否需认证头」 | ❌ `ping/time` **完全无鉴权** → `run/cloud-side-trios-verification.md` |
| `clock-deadlock-resolution.md`「路线 C 替代 timefix.sh」 | ⚠️ 精确化为**互补**：`timefix.sh` 破 TLS 死锁（第一步），`ping/time` 精校（第二步）→ §3.8 |
| `clock-deadlock-resolution.md`「CLK-1 `matool_*` 是否有 timesync 子命令」 | ⬜ 仍是流水项，但**已不阻塞**（`ping/time` 可 GET 直调） |
| `inject-relay-text-buffer-replay.md` §4「代码改动未提交，等定夺」 | → 并入 **B-09**（工作区改动仍在，须先修顺序陷阱） |
| `AGENTS.md`「`mediaplayer` 崩后不 respawn ⇒ 永久静默失效」 | ❌ `retry=0` = 无限重启（会自愈）→ §3.7 |

---

## 8. 一句话导航

- 接下来做什么 → §2（B-01…B-10）；按批次施工 → §4；
- 某条为什么这么设计 → §3 对应小节；
- 哪些别再碰 → §5；哪些已闭环 → §6；
- 旧文档的某句话还算数吗 → §7；
- 设备接口参数/取值域 → `docs/devices/lx06/`；
- 逆向侧还剩什么活 → `D:\_work\dev\reverse\lx06-firmware-reverse\run\plan\ROADMAP.md` §3。
🔴 **取回 TTS 音频一律 `ssh … 'cat <path>'`；`nano_httpd` 8888 为「取完即删」的一次性通道，禁用。**
每次成功 GET（含中途断连）后它会 `unlink` 请求的文件本身（`nano_httpd` 的 `sub_10FD0:0x116d0`；原厂
`player_play_url http://localhost:8888/…` 也在播放开始前就删），同一文件第二次恒 `000` ⇒ **无重试窗口**。
取证：`docs/devices/lx06/tts-audio-direct-fetch.md` §0.0 · 逆向侧 `run/device-local-http-and-event-source-correction.md` §1.4。

