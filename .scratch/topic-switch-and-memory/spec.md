# Spec: bridge 话题切换信令 + 对话记忆文件（对接 aurora 会话复用）

Status: ready-for-agent

关联 spec：aurora 仓库 `.scratch/upstream-session-reuse/spec.md`（服务端侧；本 spec 是其客户端配套，两者接口契约一致，实现可并行）。

## Problem Statement

bridge 当前与 aurora（OpenAI 兼容后端）的对话链路存在三个问题：

1. **上下文断裂无感知**：bridge 本地只保留最近 20 条消息的滑动窗口（`history_max_messages`），且 aurora 侧即将改为「只发本轮增量 + 30min TTL 会话复用」。TTL 过期后 aurora 侧服务端会话已失效，模型失忆，用户说「我们昨天说到哪了」得不到有意义回应——而 bridge 没有任何持久化记忆来弥补。
2. **换话题无信令**：用户说「换个话题」时，bridge 无法通知后端开新会话——旧话题上下文会污染新话题（aurora 侧新信令通道 `X-Session-Action: new` 已在服务端 spec 定义，bridge 未接入）。
3. **会话头未对齐**：bridge 现发 `X-Hermes-Session-Key` 头（Hermes 专用约定），aurora 会话复用键读取 `X-Session-Key`——不接入则小爱对话不进会话池，复用收益为零。

## Solution

bridge 侧三件事：

1. **接入 aurora 会话信令协议**：请求头改为（或叠加）`X-Session-Key`；「换个话题」暗号触发时清本地历史 + 下一轮请求带 `X-Session-Action: new`，两步原子执行。
2. **暗号方案 B**：整句精确「我们换个话题」+ 模糊词表（≤8 字、编辑距离 ≤2）双通道识别，触发后 TTS 直接播过渡语（不发 LLM），零延迟反馈。
3. **对话记忆文件**：把每轮对话（时间戳、user、assistant）追加写入 NAS 本地 JSONL 记忆文件；TTL/新会话导致的上下文断档由 bridge 在发请求时**自行决定**是否注入记忆摘要（bridge 是上下文唯一权威，aurora 不猜）。

## User Stories

1. As a 小爱语音用户, I want 说「我们换个话题」后立即听到过渡语并进入全新话题, so that 新对话不被旧上下文污染且无需等待模型响应。
2. As a 小爱语音用户, I want 说「换个话题」「聊点别的」等相近说法也能换话题, so that 不必背诵严格口令。
3. As a 小爱语音用户, I want 闲聊「我想给你讲个换话题的笑话」这类长句子**不**触发换话题, so that 暗号不会误拦截正常表达。
4. As a 小爱语音用户, I want 隔天再对话时模型还记得我们昨天聊过大方向, so that 长期使用有连续感。
5. As a 小爱语音用户, I want 对话内容持久化在本地 NAS 而非发送给第三方, so that 隐私留在家庭内网。
6. As an 运维者, I want 记忆文件按日期滚动、可配置保留天数, so that 磁盘不无限增长。
7. As an 运维者, I want 记忆注入不拖慢对话首字延迟, so that 体验不变差。
8. As a bridge 开发者, I want 换话题时「清历史」与「发信令」原子完成, so that 不出现 bridge 认为新话题、aurora 还在旧会话的半态。
9. As a bridge 开发者, I want 暗号词表与过渡语可配置, so that 不改代码就能调整触发词与播报文案。
10. As a bridge 开发者, I want 后端切换到非 aurora 预设（官方 DeepSeek API 等）时信令头被安全忽略, so that 同一套 bridge 代码兼容所有后端预设。

## Implementation Decisions

### 会话头对齐（对接 aurora `X-Session-Key`）

- `OpenAIManager._headers()` 在现有 `session_header` 机制上叠加发送 `X-Session-Key: <session_key>`（保留 `X-Hermes-Session-Key` 兼容 Hermes；两头并存，值相同）。具体头名取舍实现时确认 aurora 侧最终读法后对齐。
- 会话键即现有 `session_key`（`agent:default:open-xiaoai-bridge` 格式），不新增键体系。

### 暗号方案 B（用户拍板）

- 配置块（`config.py` 的 openai 段新增）：

  ```python
  "new_topic": {
      "exact": ["我们换个话题"],
      "fuzzy": ["换个话题", "聊点别的", "换个话题吧"],
      "transition": "好的，我们聊点别的。想聊什么？",
      "enabled": True,
  }
  ```

- 识别规则（在 `external_conversation.py` 每轮 ASR 文本出口处、exit_keywords 检查之后）：
  - **精确通道**：文本去标点去空白后 == exact 中任一词。
  - **模糊通道**：文本长度 ≤8 字（去标点后）且与 fuzzy 词表中某词编辑距离 ≤2。
  - 两通道都不命中 → 正常发 LLM。长句（>8 字）永不进模糊通道——这是防误拦截的核心门槛。
- 触发后动作序列（**同一临界区原子执行**）：
  1. `OpenAIManager.reset_session()` 清 bridge 本地对话历史；
  2. 置「下轮请求带 `X-Session-Action: new`」标志；
  3. TTS 直接播 `transition` 文案（不发 LLM，省 2–4s 模型延迟）；
  4. 不退出连续对话循环——用户紧接着说新内容即新话题第一轮。
- ASR 误转写容忍：fuzzy 词表含常见误转写（如「换个画图」距离 1 自动命中），无需单独枚举。

### X-Session-Action 信令传递

- `OpenAIManager` 增加一次性标志（如 `_force_new_session`）：暗号触发置位 → 下一次 `_headers()` 读取并附加 `X-Session-Action: new` → 发送后立即清位（one-shot）。
- 非 aurora 后端忽略未知头，无副作用（user story 10）。

### 对话记忆文件

- **写入**：每轮对话完成后（`_append_history` 同时机）追加 JSONL 一行：`{"ts": ISO时间戳, "session_key": ..., "user": ..., "assistant": ...}`。
- **位置**：bridge 容器卷内（NAS 持久化），路径可配置，默认 `data/memory/<session_key>.jsonl`（按 session_key 分文件，与历史隔离语义一致）。
- **滚动**：按天滚动 + 保留天数可配置（默认 30 天），写入时惰性清理过期文件。
- **读取与注入**（TTL/换话题后首请求时）：
  - 触发条件：检测到「上一轮距今 >30min」或「刚发生换话题」，且记忆文件非空。
  - 注入方式：取最近 N 轮（默认 5 轮、可配置）拼成简短上下文前缀（system 角色或首条 user 前缀，实现时定），形如「此前对话摘要：用户聊过 A/B/C……」。注入内容走 LLM 的常规请求，不新增请求。
  - 延迟预算：文件读取为本地 NAS 磁盘 IO（毫秒级），不引入可感知延迟（user story 7）。
  - 注入不做语义压缩/LLM 摘要——纯文本截取最近 N 轮，简单可预期（复杂摘要留作后续增强）。
- **隐私边界**：记忆文件只落 NAS 本地，不随对话请求整体外发（仅注入摘要片段），不进 git（`.gitignore` 排除 `data/memory/`）。

### 不做的事

- bridge 不实现 aurora 侧的会话池/TTL 逻辑——那些是 aurora 的职责；bridge 只负责信令与自身记忆。
- 不改动 openclaw/xiaoai 另两条连续对话策略——本 spec 只覆盖 OpenAI 兼容路径（`openai_conversation.py`）。

## Testing Decisions

- **测试 seam**：`OpenAIManager` 的公开行为（headers 构造、信令 one-shot 语义、reset+置位原子性）与暗号识别函数（纯函数：文本 → 是否触发）。不 mock aiohttp 层。
- 暗号识别：表驱动单测覆盖——exact 命中、fuzzy 距离内命中、长句不误触、标点干扰、ASR 误转写样例。
- 信令：one-shot 断言（置位 → 首次 headers 消费 → 自动清位）；与 `reset_session` 的原子性用同线程顺序断言。
- 记忆文件：JSONL 追加/滚动/过期清理/注入截取的纯函数测试（tmp 目录）；不测 NAS 挂载本身。
- 暗号→TTS→信令的端到端行为：现有 bridge 测试基建内做 controller 级验证（参照 `bridge/tests/test_admin_panel.py` 风格）；真机语音验收留给部署后人工单次。

## Out of Scope

- aurora 侧会话池/TTL/信令处理（aurora 仓库 spec 负责）。
- openclaw / xiaoai 原生两条连续对话策略的暗号与记忆接入。
- 记忆的 LLM 语义摘要/向量化检索（当前为最近 N 轮纯文本截取）。
- 跨 session_key 的记忆共享（每个 session_key 独立记忆文件）。
- ASR 引擎替换或唤醒词链路改动。

## Further Notes

- 延迟收益依赖 aurora 侧先行或同期落地：bridge 单方面接入信令无副作用（多余头被忽略），但复用加速要等 aurora 就绪。
- 暗号触发后的 transition 直接 TTS 是有意的体验设计：省一轮 LLM 往返（2–4s），且模型不会自由发挥。
- 「换个话题」若恰为用户想聊的内容（如「换个话题怎么用英语说」=9 字，超模糊通道长度上限且非 exact 整句）不会误触——8 字门槛是防误拦的核心，调整词表时勿放宽。
- 部署注意：bridge 以 Docker 跑在 NAS（`/volume2/docker/open-xiaoai-bridge`），记忆文件目录需在 compose 卷映射中确保持久化。
