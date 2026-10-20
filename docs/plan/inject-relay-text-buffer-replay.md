# 建议：`InjectRelay` 补窗口期文本缓冲，修 WS 晚连丢 ASR 文本

> 本文的「待办 / 授权 / 排序」已并入 [`README.md`](README.md)（**项目侧计划唯一权威**）；本文只保留**深度取证与方案细节**，过时断言已在 `README.md` §7 取代链登记。
> 状态：🟢 **已被取代（2026-10-21）**——最终实现改用 `seq` + `?since=` 断点续取，**不是**本文的 `text_buffer` 方案。原因见 §5。
> 本文保留为**问题取证与推理过程**（问题本身成立，§1–§2 仍有效）。
> 现行实现与状态追踪 → [`README.md`](README.md) **B-09**。
> 提出时间：2026-10-19
> 关联：[`asr-input-reliability.md`](asr-input-reliability.md) §WS 重连窗口（A1–A4 同族问题）

---

## 1. 问题

`InjectRelay` 的 `publish_text()` 只做**实时广播**：

```python
for q in list(self.text_queues):
    q.put_nowait(text)
```

`text_queues` 里的订阅者是 **WS 连接时才创建**的。

⇒ **WS 订阅者连接晚于 ASR 结果产生时，那段文本永久丢失**。

### 1.1 触发条件（非边角）

推流（`open_session` → 上游推音频）到 ASRT 出结果往往在**同一瞬间**完成；
而 WS 客户端的连接/订阅有网络与调度延迟。
⇒ 这个窗口**每次都会命中**，不是罕见竞态。

表现：HA 侧「喊了指令但音箱/面板没反应」，而 bridge 日志显示
`[InjectRelay] texts=N`（N>0）——**bridge 以为发出去了，实际没人收到**。

---

## 2. 与 asr-input-reliability.md 的关系

该文 §WS 重连窗口记的是 **client 侧**丢事件（`position = metadata.len()`）。
本文记的是 **bridge 侧**丢事件——**同一个用户可见症状的第二个独立根因**。

| 侧 | 根因 | 已有措施 |
|---|---|---|
| client | monitor 重启时 `position = len()` | A1–A4（减少重启/标记/对齐时间戳） |
| **bridge** | **WS 订阅晚于 ASR 结果 ⇒ 广播无人接收** | **无**（本文建议补） |

---

## 3. 建议改动（10 行）

```python
class InjectRelay:
    def __init__(self):
        ...
        self.text_queues: list[asyncio.Queue] = []
        self.text_buffer: list[str] = []      # 新增：窗口期文本缓冲

    def open_session(self):
        self.active = True
        self.text_buffer.clear()              # 新窗口清上一窗
        ...

    def publish_text(self, text: str):
        if not self.active or not text:
            return
        self.stats["texts"] += 1
        self.text_buffer.append(text)         # 新增：进缓冲
        for q in list(self.text_queues):
            q.put_nowait(text)

    # WS handler 内，_notify_state(ws) 之后：
    for t in self.text_buffer:                # 新增：重放
        await ws.send_json({"event": "text", "text": t})
```

### 3.1 设计要点

| 点 | 处理 |
|---|---|
| 缓冲生命周期 | 绑定 `open_session`/`close_session` 窗口，不跨窗口累积 |
| 与实时队列的关系 | **重放 + 实时并存**：晚连者先拿历史，再收实时，不重不漏 |
| 内存上界 | 一个窗口的 ASR 文本量很小（每条 ≤ 数百字节 × 数十条） |
| 重复风险 | 无——但**必须保证重放先于实时队列投递**，见 §3.2 |

### 3.2 ⚠️ 顺序陷阱：重放必须早于 `push_task`

现有工作区代码把 `push_task = asyncio.create_task(self._push_texts(ws, q))`
放在**重放之前**：

```python
push_task = asyncio.create_task(self._push_texts(ws, q))   # ← 先建
await self._notify_state(ws)
for t in self.text_buffer:                                  # ← 后放
    await ws.send_json({"event": "text", "text": t})
```

`create_task` 只是**调度**，任务要等当前协程让出控制权才真正跑。
后续的 `await ws.send_json(...)` 会 yield ⇒ **`_push_texts` 可能在重放中途插进来**，
若此刻队列里已有实时文本，就会**先收到实时、后收到历史 ⇒ 乱序**。

**正确写法**：把重放挪到 `push_task` 创建之前，并让 `_notify_state` 留在最后
（或先重放、再建任务）：

```python
q: asyncio.Queue = asyncio.Queue(maxsize=64)
self.text_queues.append(q)
await self._notify_state(ws)
for t in self.text_buffer:              # ① 先重放历史
    await ws.send_json({"event": "text", "text": t})
push_task = asyncio.create_task(self._push_texts(ws, q))   # ② 再开实时
```

或者更稳：让 `_push_texts` 内部等一个 `started` 事件。
⇒ **这是当前工作区改动的已知缺陷，保留前必须先修**。

---

## 4. ⚠️ 状态（2026-10-21 更新：本文方案已被取代）

本文的 `text_buffer` 方案**未采用**。最终实现见 §5。

（历史：`core/services/api_server.py` 曾留有本文对应的未提交改动；
已于 2026-10-21 被 `seq` + `?since=` 方案重写并提交。）

---

## 5. 🟢 最终实现（2026-10-21）：为什么不用 `text_buffer`

本文 §3.2 已识别「顺序陷阱」，但还有**第二个更根本的缺陷**：

```python
def open_session(self):
    self.text_buffer.clear()   # ← 开新窗口就清空历史
```

**PC 在窗口结束之后才连 WS 是正常操作**（松开快捷键 → 工具才建立连接）。
此时上一窗口的文本已被清掉 ⇒ **本文方案害的正是它想修的那类场景**，
只是把时间窗从「几十毫秒」拉长到「一个会话」。

因此改为 **拉取（pull）语义**：

| 模型 | 例子 | 晚到者 |
|---|---|---|
| push（广播） | 原 `publish_text` 的 `text_queues` | ❌ 永久丢失 |
| pull（带序号） | 现 `?since=N` + `_text_log` | ✅ 可补拉 |

实现要点：

| 机制 | 作用 |
|---|---|
| `_seq` 单调递增、永不复位 | 去重 / 排序 / 断点续取 |
| `_text_log` 有界裁剪（`MAX_TEXT_LOG=200`） | 内存有界，且**不随窗口清空** |
| `GET /api/inject/text?since=N` | 只回放 `seq > N` |
| 每帧带 `seq` | 实时/回放同一序号空间，乱序可被纠正 |
| 先回放、后订阅实时队列 | **结构性**消除 §3.2 的顺序陷阱 |

⚠️ **消费端尚未实现**：全仓检索确认 `/api/inject/text` 目前**无任何消费方**。
（aurora 是 NAS 上的对话后端，与注入链路正交，**不订阅此 WS**。）
未来 PC 侧输入工具实现时，应记最后 `seq` 并在重连时带 `?since=`。

测试：`tests/test_inject_text_replay.py`（13 条，纯内存 asyncio，不出声、不碰设备）。
