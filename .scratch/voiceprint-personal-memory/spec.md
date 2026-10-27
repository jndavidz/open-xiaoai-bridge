# Spec: 声纹识别 + 家庭成员个人档案 / 个人记忆（本地方案）

Status: ready-for-agent

> 跨批台账：本 spec 已登记于 `ws: docs/plan/pending-followups.md` §待启动（未拆票）。
> 与 B-30 的「对话记忆」区分：本文的「个人档案（person profile）」是稳定可编辑的画像
>（称呼、喜好、禁忌、语气、角色），B-30 记忆是逐轮原文摘要；两者并存、互不替代。

## Problem Statement

家里所有人共用 `agent:javis:home` 一个会话键，模型不知道对面是谁：

1. **称呼与人设错配**：大人、孩子、老人得到同一套语气与称呼，不能按人调整（孩子要更简短、更安全的边界；老人要更慢更直白）。
2. **偏好不复用**：模型每次都要重新问「你爱吃什么/讨厌什么」，用户要重复交代；B-30 的对话记忆是最近 5 轮原文，会滚动丢失，且不构成稳定画像。
3. **记忆串户**：多人共用一个 session_key，B-30 记忆文件也共用一份；孩子能在下一轮被模型带到成人的话题与称谓里（隐私与观感问题）。
4. **设备原生声纹用不上**：LX06 固件里 `VoiceprintRecognizeResult` 的 handler 未实现，client 只透传 ASR 文本，bridge 拿不到说话人身份（判据见 Further Notes）。注册还属隐私红线（声纹+支付）。

## Solution

在 bridge 本地做「说话人识别 → 会话键与档案路由」：

1. **本地声纹识别**：每轮用户语音取一段 16k PCM，算说话人嵌入向量，与已注册成员质心比对；置信度与领先幅度同时达阈才算命中。
2. **命中即路由**：该轮切换到该成员的会话键（默认 `agent:<person>:home`），并把其个人档案作为一条 system 消息注入本轮请求；按人隔离的对话历史与 B-30 记忆文件随之自动成立。
3. **未知不猜**：低置信、领先不足、未注册声纹、模型不可用 → 保持默认共享会话（现状行为），只在日志留判定。
4. **注册即用**：语音引导式注册（三段提示、每段一句话），生成质心与内聚度评分；档案文件可手工编辑、可停用、可删除。
5. **先影子后路由**：`mode=shadow` 只记录「若按此配置会切到谁、分数多少」，真机观察准确率达标后再切 `route`。

## User Stories

1. As a 家庭用户, I want 唤醒后模型按我的称呼回应，so that 对话不像在跟通用客服讲话。
2. As a 家庭用户, I want 我喜欢的/讨厌的事只交代一次，so that 之后每次对话不必重复说明。
3. As a 家长, I want 孩子对话使用孩子自己的档案与历史，so that 孩子不会被带到成人的话题与称谓里。
4. As a 家长, I want 孩子的档案可以写明年级与内容边界，so that 辅导口吻与安全边界稳定生效。
5. As a 家庭用户, I want 客人（未注册）说话时正常可用，so that 有外人在场也不影响使用。
6. As a 家庭用户, I want 认不出我时不要乱猜，so that 宁可退化到默认也不串人。
7. As a 家庭用户, I want 说话后立刻得到响应，so that 识别不带来可感知的等待。
8. As an 运维者, I want 用语音提示完成一人注册（三段、每段一句），so that 不必用手机或抓音频文件。
9. As an 运维者, I want 注册结果返回每段相似度与内聚度，so that 我能判断这次注册质量是否合格。
10. As an 运维者, I want 重注册覆盖旧声纹，so that 嗓音变化或注册失败可以重来。
11. As an 运维者, I want 单独停用某成员声纹而保留档案，so that 临时禁用不影响其他人。
12. As an 运维者, I want 删除某成员的全部声纹与档案数据，so that 隐私诉求能一步落实。
13. As an 运维者, I want 成员列表看到启用态与最近判定，so that 我能排查为什么没切过去。
14. As an 运维者, I want 先跑影子模式看准确率，so that 上线前有数据支撑阈值。
15. As an 运维者, I want 阈值与领先幅度可配置且热重载，so that 调参不用重建镜像。
16. As an 运维者, I want 识别事件有可核对的日志（谁、分数、是否切换），so that 线上问题可复盘。
17. As an 运维者, I want 音频不留存，so that 声纹能力不引入新的录音留存面。
18. As an 运维者, I want 模型缺失或加载失败时整体降级为现状，so that 依赖缺失不阻断对话。
19. As a bridge 开发者, I want 身份路由与后端类型解耦，so that 换 aurora / 官方 DeepSeek 不影响识别。
20. As a bridge 开发者, I want 显式人设（如「你好助教」）优先于声纹档案，so that 用户明确选择时不被声纹覆盖。
21. As a bridge 开发者, I want 每人会话键可配置，so that 命名与既有 `agent:*:home` 约定一致。
22. As a bridge 开发者, I want 注册/识别/路由三层各自可单测，so that 调参不靠真机碰运气。
23. As a 家庭用户, I want 多人同时说话时按主说话人处理，so that 嘈杂场景不产生随机切换。
24. As a 家庭用户, I want 换人说话时该轮即切到对应档案，so that 不必重新唤醒或重启对话。
25. As an 运维者, I want 档案内容只在需要时注入，so that 不命中的人不会看到别人的画像。

## Implementation Decisions

### 术语（本 spec 引入，落地时同步 bridge GLOSSARY）

- **声纹身份（speaker identity）**：由语音嵌入判定出的家庭成员，附置信度。
- **个人档案（person profile）**：稳定的、可编辑的画像文本（称呼/喜好/禁忌/语气/角色），按人一份。
- **注册（enrollment）**：采集若干句语音生成本人质心并向档案绑定。
- **影子模式（shadow mode）**：只记录判定、不改变会话键。

### 识别引擎与运行时

- 复用容器内已有 `sherpa_onnx` 的 `SpeakerEmbeddingExtractor`（实测 1.12.28 可用，onnxruntime 1.23.2 就绪），不引入新运行时依赖。
- 模型：中文 16k 说话人嵌入模型（CAM++ / 3D-Speaker 系，约 28MB），落在 `core/models/`（gitignore），下载按工作区口径走 Gopeed。
- 推理离线程池（executor），不阻塞事件循环。
- 引擎可注入：模块对外只暴露「PCM bytes → 嵌入向量」，测试用 fake extractor。

### 音频采集与分割

- 采集点：`XiaoAI.on_input_data` 完成下混后的 1ch PCM（即进入 `GlobalStream` 之前/同一位置）。
- 环形缓冲：内存内有界缓冲（默认 8s、100ms 粒度），提供「最近 N 秒」读取；**不做任何落盘**。
- 取段时机：每轮 ASR 文本定稿时，从缓冲尾部按能量/VAD 取最后一段语音（不足则整段）；注册流程复用同一取段逻辑（由 TTS 提示音分隔）。
- 隐私：缓冲在取段后不清空由有界容量自然覆盖即可；不上传、不入日志。

### 每轮路由（核心）

- 识别点：`_acquire_user_text` 返回文本之后、发送后端之前（本地 ASR 与原生 ASR 两条输入链路共用同一位置）。
- 决策输入：声纹嵌入、已注册成员集合、配置阈值。
- 决策输出三态：`person`（命中）/`unknown`（不命中）/`disabled`（功能关闭或引擎不可用）。
- `person` + `mode=route`：切换到该成员会话键并激活其档案注入；
  `unknown` + `mode=route`：切回默认会话键（防止继续沿用上一位成员的历史与画像）；
  `mode=shadow` 或 `disabled`：不改会话键，仅日志。
- 显式人设优先：本次唤醒由 PERSONAS 条目路由（如「你好助教」）时，声纹只做记录，不改变会话键与档案。
- 会话键默认 `agent:<person>:home`，允许在档案中覆盖；切换走既有泛化门面（`app.set_session_key`），不新增门面方法。

### 档案与注入

- 存储：`data/profiles/<person>.json`，含展示名、会话键、档案文本字段、启用态、质心向量、样本数、更新时间、最近判定摘要。删除成员 = 删该文件。
- 注入：`_build_messages` 在会话人设之后、B-30 记忆摘要之后追加一条 system「当前说话人档案」，仅当本轮命中且档案启用；未命中不注入。
- 档案字段面向语音场景控制长度（总长有上限），超限截断并告警，绝不阻断请求。
- 不自动从对话中学习/改写档案（见 Out of Scope）。

### 注册与管理接口

- 注册：管理面新增一个「注册一人」的阻塞式端点（person id + 展示名），内部经 TTS 播三段提示、按提示音分段取音频、逐段嵌入、生成质心与内聚度，超时（默认 30s）失败并回报已采集段数。
- 管理：成员列表（启用态、内聚度、最近判定）、停用/启用、删除；沿用既有后台面板鉴权与写类端点鉴权口径，不新增鉴权体系。
- 配置块：`openai.voiceprint = {enabled, mode, threshold, margin, model_path, buffer_seconds, profile_max_chars}`，随现有配置热重载生效；默认 `enabled=false`（灰度前零行为变化）。

### 降级与观测

- 模型缺失、加载失败、嵌入异常：一条 WARN（含原因），功能整体降级为现状，不抛到对话链路。
- 日志：每次判定一行（轮号、person、score、margin、是否切换、原因），不含音频与对话原文。
- 与 B-30 的关系：会话键按人切分后，`data/memory/` 自然按人分文件；不改 B-30 的写入与注入逻辑。

### 已知取舍

- 识别在最坏情况下给发送前增加一次嵌入推理（N3060 双核粗估 0.2–0.4s）；真机测量后再决定是否缩短窗口或改量化模型。
- 没有 diarization：多人同时说话按主说话人处理，不切分。
- 阈值需真机标定（亲子音色相近为主要风险），默认配置保守、宁可 unknown。

## Testing Decisions

- **好测试的定义**：只断言外部行为——本轮最终使用哪个会话键、注入了哪段 system 内容、日志/判定结果是什么；不断言余弦实现细节、不断言内部函数调用顺序。
- **唯一的 feature seam**：连续对话的「轮边界」——即 `_acquire_user_text` 之后的身份决策与随后的档案注入。身份以可注入依赖提供（固定 PCM fixture + 确定性 fake extractor），backend 用 stub 记录 session_key 与 messages。
  - prior art：`tests/test_new_topic_wiring.py`（控制器级 + stub backend + 原子次序断言）、`tests/test_conversation_memory.py`（模块级纯函数 + tmp 目录）。
- **覆盖矩阵**（同一 seam）：
  1. 命中 + `route` → 切到成员键、注入该成员档案、日志判定
  2. 未命中/低分 → 不切键、不注入
  3. 领先不足（第二名接近）→ 视为 unknown
  4. `shadow` → 判定可见、键不变
  5. `disabled` / 模型失败 → 与现状逐字等价（无键切换、无注入、一条 WARN）
  6. 显式人设（tutor）优先 → 键与档案不被声纹覆盖
  7. 上一轮命中 A、本轮 unknown → 回默认键（不残留 A 的档案与历史）
  8. 档案文件缺失/损坏 → 该成员视为未注册，不阻断
- **纯函数层**（表驱动，prior art：`tests/test_new_topic.py`）：质心构造、余弦与阈值+领先幅度判定、成员档案解析与校验、档案块拼装与截断、取段边界（空缓冲/过短/无语音）。
- **注册状态机**：模块级测试（fake 提示播报 + 注入分段 PCM），覆盖 3 段成功、中途超时、单段失败重试、重复注册覆盖；HTTP 包装层不单独做集成测试。
- **真机项（需人声，部署后人工）**：3 人各注册一次；影子模式一周准确率与误认率（成员间、成员 vs 客人和电视）；首音延迟对比；删除/停用即时生效。

## Out of Scope

- 设备原生声纹通道（ubus `voice_print`、AIVS `VoiceprintRecognizeResult`、声纹支付）。
- 云端声纹服务或第三方声纹 API。
- 多人同时说话的 diarization / 逐句分人转写。
- 从对话中自动学习或改写档案（LLM 抽取画像、向量化检索）。
- 跨家庭、跨设备的声纹与档案同步。
- 儿童内容审核策略本身（本 spec 只提供档案字段承载年级与边界）。
- B-30 对话记忆的语义摘要/向量检索（继续纯文本截取）。
- openclaw / xiaozhi 两条连续对话策略的身份接入（M1 只 OpenAI 兼容路径，与 B-30 一致）。

## Further Notes

- **实机串户样本（2026-10-27 语音验收窗口）**：全家共用 `agent:javis:home` 一个会话键与一份记忆文件。10:30:55 一轮「我今天让你放过什么歌」，模型答出《晴天》/「切了两次下一首」/《恒星不忘》—— 这些内容只存在于当日记忆文件、不在本轮进程历史里（注入路径本身已验证工作正常）。也就是说**任何家庭成员都能把别人（含成人）先前说过的内容套出来**。这把本 spec 的动机从「按人定制语气」升级为「隐私边界」，也是 `mode=route` 上线前必须先有保守阈值与 unknown 回退的直接理由。
- **原生路线为何不可用（置信度高）**：设备确有 `voice_print` ubus 6 法与 AIVS `SpeechWakeup.Wakeup.recognize_voice_print` / `SpeechRecognizer.VoiceprintRecognizeResult`；但固件 handler 清单把 `VoiceprintRecognizeResult` 列为未实现（`voice_print.get_status` 实测 `REGISTER_FAIL`），client 只把 `instruction.log` 的 ASR 文本透传，bridge 拿不到身份；且 `register_voice_*` 属隐私红线。
- **运行时事实（2026-10-27 实测）**：容器 venv 内 `sherpa_onnx 1.12.28` 含 `SpeakerEmbeddingExtractor`，`onnxruntime 1.23.2`；NAS 为 Celeron N3060 / 2 核 / 8GB（现用约 2.9GB），常驻内存预估 50–100MB。
- **与 B-30 的接口**：身份决定会话键，会话键决定记忆文件；不新增第二套记忆存储。档案（画像）与逐轮记忆并存，注入顺序：会话人设 → 档案 → 记忆摘要。
- **顺带发现的缺陷（不在本 spec 修）**：B-30 记忆行的 `user` 字段当前写入了 `_with_rule_prompt` 追加的输出纪律文本，注入时会回灌噪音；建议另立小票把「发给模型的增强文本」与「落记忆的原始文本」分离。
- **落地时需同步**：bridge `GLOSSARY.md`（声纹身份/个人档案）、`AGENTS.md` 目录树与边界约束、`docs/plan/pending-followups.md` 本条状态、部署手册（模型文件与首次注册步骤）。
