# 建议：`InjectRelay` 补窗口期文本缓冲，修 WS 晚连丢 ASR 文本

> 状态：**待用户定夺**（代码改动**未提交**，见 §4）
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

## 4. ⚠️ 当前状态：代码改动未提交

`core/services/api_server.py` 已有上述改动的**工作区版本**（`git diff` 可见），
但：

- 用户只授权「建议落到 `bridge/docs/plan`」，**未授权改 bridge 代码**；
- 因此该改动**保留未提交状态**，等用户定夺：**保留并提交** / **回退**。

### 4.1 若要回退

```bash
cd bridge && git checkout -- core/services/api_server.py
```

### 4.2 若要保留

需补：
1. **先修 §3.2 的顺序陷阱**（重放挪到 `push_task` 之前）；
2. 一次真机/容器验证（WS 晚连场景：先推流后连 WS，确认收到历史文本且顺序正确）；
3. 补一条说明到 `bridge/AGENTS.md` 的坑清单（WS 订阅晚于 ASR ⇒ 靠 text_buffer 兜底）。
