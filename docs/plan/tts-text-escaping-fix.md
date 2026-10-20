# 🔴 TTS 文本 JSON 转义缺陷：完整取证与修法

> 本文的「待办 / 授权 / 排序」已并入 [`README.md`](README.md)（**项目侧计划唯一权威**）；本文只保留**深度取证与方案细节**，过时断言已在 `README.md` §7 取代链登记。
> 状态：**待用户定夺**（`speaker.py:75`，10 行改动）
> 索引：[`pending-decisions.md`](pending-decisions.md) §2
> 上游取证（固件侧）：`D:\_work\repos\open-xiaoai\docs\devices\lx06\tts-text-escaping-defect.md`

---

## 1. 缺陷定位

### 1.1 两条 TTS 路径，一条安全一条有洞

```python
# core/services/speaker.py

# 路径 A：blocking=True  ← 🔴 有洞
command = f"/usr/sbin/tts_play.sh '{text.replace("'", "'\\''") or '你好'}'"

# 路径 B：blocking=False  ← ✅ 安全
data = json_encode({"text": text or "你好", "save": 0})
command = f"ubus call mibrain text_to_speech '{data}'"
```

路径 B 的 `json_encode` 就是 `json.dumps(obj, ensure_ascii=False)`
（`core/utils/base.py:22`）⇒ `"` 与 `\` 都被正确转义。

### 1.2 路径 A 的洞在哪

```sh
# usr/sbin/tts_play.sh:5
local _result=$(ubus call mibrain text_to_speech "{\"text\":\"$1\",\"save\":1}")
```

`$1` 被**直接插进 JSON 字符串字面量**，无 JSON 转义。

### 1.3 纯构造复现（未执行）

```python
text = '他说"你好"然后走了'
esc  = text.replace("'", "'\\''")     # bridge 的转义：无变化
# 最终命令：
ubus call mibrain text_to_speech '{"text":"他说"你好"然后走了","save":1}'
#                              ^^^^^ 字面量在这里就结束了
```

**JSON 破裂。**

### 1.4 哪些字符触发

| 字符 | shell 层 | JSON 层 | 结果 |
|---|---|---|---|
| `"` | 无需转义 | **需转义** | 🔴 破裂/截断 |
| `\` | 无需转义 | **需转义** | 🔴 破裂/截断 |
| 换行 / Tab | 保留 | 需转义 | 🔴 破裂 |
| `'` | ✅ 已转义 | 无需 | 安全 |
| 中文 / emoji | 保留 | 保留（`ensure_ascii=False`） | 安全 |

⇒ **触发条件：TTS 文案含英文双引号、反斜杠或换行。**

### 1.5 为什么一直没暴露

bridge 的播报文案（HA↔音箱 TTS、场景播报）多为中文短句，
不含 `"` / `\` ⇒ **潜伏缺陷，非活跃故障**。

⇒ 但也意味着：**任何新加的、内容来自用户输入或 LLM 生成的播报文案，
都可能随机触发**（LLM 输出含引号极常见）。

### 1.6 运行时实际行为（TTS-E1 已定案）

**2026-10-19 真机实测已闭合**：**只有报错，没有截断**。

```
输入含 " 或 \ ⇒ rc=255 + stderr "Failed to parse message data"
⇒ 不生成任何音频，请求根本没到 mibrain（instruction.log 零新增）
```

曾列两种可能（① 报错 ② 容错解析成 `{"text":"他说"}` 而静默截断），
**② 不存在** —— 3 种 payload 变体（`save` 在前/在后/不传）全部 `rc=255`，blobmsg 严格拒收。
补充：换行/Tab（真控制字符或 `\n`/`\t` 字面量）**都不触发**，判据是**JSON 合法性**而非字符本身。
取证：`run/tts-e1-runtime-verification.md`。

---

## 2. 修法

### 2.1 最小修（保持 blocking 语义）

```python
# core/services/speaker.py:75
_raw = text or "你好"
# ① 先做 JSON 转义（顺序不能反！）
_safe = (_raw
         .replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t"))
# ② 再做 shell 单引号转义
_safe = _safe.replace("'", "'\\''")
command = f"/usr/sbin/tts_play.sh '{_safe}'"
```

⚠️ **顺序至关重要**：先 JSON、后 shell。
反了的话，JSON 转义产生的 `\"` 会被 shell 层破坏。

### 2.2 更稳（绕开双层转义）

改用路径 B 已安全的 `json_encode`，自己完成「合成 → 取路径 → 播放」：

```python
data = json_encode({"text": text, "save": 1})       # save=1：落盘并返回路径
res = await self.run_shell(f"ubus call mibrain text_to_speech '{data}'")
path = json_decode(json_decode(res.stdout)["info"])["path"]
await self.run_shell(f"miplayer -f '{path}'")
```

**优点**：彻底消除 shell 套 JSON 的双层转义问题；
且复用了 `tts_play.sh` 已验证的 `save=1` 路径。

**代价**：改变了 blocking 语义（需评估调用方），且要处理临时文件清理
（`tts_play.sh` 会 `rm $_path`，自己实现也要删）。

---

## 3. ⭐ 顺带发现：`tts_play.sh` 用 `save:1`

```sh
ubus call mibrain text_to_speech "{\"text\":\"$1\",\"save\":1}"
...
miplayer -f $_path
rm $_path
```

与 bridge 路径 B 的 `save:0` **不同**：

| 路径 | `save` | 行为 |
|---|---|---|
| `tts_play.sh`（blocking） | **1** | 合成 → 落盘 → `miplayer -f` → `rm` |
| bridge 路径 B | 0 | 合成 → 设备自己播 |

⇒ **正面印证 TTS 直取路径**：`save=1` 会让 `text_to_speech` 在
`info.path` 里给出**音频文件路径**。厂商自己就是这么用的。

⇒ bridge 若要「TTS 音频直取」，走 `save=1` **比自建 `batch_tts` 客户端
省一个数量级**（见 `docs/devices/lx06/tts-audio-direct-fetch.md` §7.1）。

---

## 4. 同类风险排查（TTS-E2）

除 `speaker.py:75` 外，是否还有别的「shell 套 JSON」路径？

| 调用点 | 是否安全 |
|---|---|
| `speaker.py:75`（`tts_play.sh`） | 🔴 **有洞** |
| `speaker.py:84`（`text_to_speech` + `json_encode`） | ✅ 安全 |
| `speaker.py:164`（`ai_service` + `json_encode`） | ✅ 安全 |
| `speaker.py:82`（`player_play_url` + `json_encode`） | ✅ 安全 |

⇒ **只有 `tts_play.sh` 这一条**，因为它把 JSON 构造放在了**设备侧脚本里**，
bridge 只能传裸文本过去。

**教训**：凡是「bridge 传文本 → 设备侧脚本拼 JSON」的路径，
都要按本文件 §1.4 的表逐字符核对。

⚠️ 另有一条**独立的**判成败陷阱：`tts_play.sh` **成功时 `rc` 也是 1**
（尾行 `[ x"$player_stat" == x"1" ] && { ... }` 短路）⇒ 调用方不得用 `exit_code` 判成败，
须改副作用回读。见 [`README.md`](README.md) B-01 / §3.1。

---

## 5. 验收标准

修完后应能通过：

```python
# 单元测试（无需设备）
cases = [
    '他说"你好"然后走了',
    'back\\slash',
    'line1\nline2',
    "it's fine",                # 单引号
    '中文 emoji 🎉',
]
for c in cases:
    # 构造出的命令必须能让 json.loads 解析出原文本
    cmd = build_tts_command(c)
    m = re.search(r"'\{\"text\":\"(.*?)\",\"save\":1\}'", cmd)
    assert json.loads('{"text":"' + m.group(1) + '"}')["text"] == c
```

---

## 6. 原「未解决」清单（三条已全部闭合）

| ID | 结论 |
|---|---|
| ~~TTS-E1~~ | ✅ **已闭合**：报错（`rc=255` 整条拒收），**无截断路径**（`run/tts-e1-runtime-verification.md`） |
| ~~TTS-E2~~ | ✅ **已排查**：全仓 4 处「shell 套 JSON」，仅 `speaker.py:75` 一处有洞（其余 3 处走 `json_encode`） |
| ~~TTS-E3~~ | ✅ **已解**：`info` 是**字符串**（内层再一层 JSON，只有 `path` 一个键），`path` 为裸路径无 `file://`；清理责任在调用方（`/tmp` 易失、不改则堆积） |
