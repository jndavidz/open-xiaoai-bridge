# 02: 暗号识别纯函数（exact + fuzzy 双通道）

Type: task
Status: resolved
Blocked by: （无）

## Body

新模块 `core/utils/new_topic.py`：`match_new_topic(text, *, exact, fuzzy) -> bool`（纯函数）。

- **精确通道**：文本去标点去空白后 == exact 中任一词。
- **模糊通道**：去标点后长度 ≤8 字 且与 fuzzy 词表某词编辑距离 ≤2。
- **长句（>8 字）永不进模糊通道**——防误拦截的核心门槛（spec：user story 3）。
- 编辑距离自实现（小规模 O(mn) DP，禁外部依赖）。

配置形状（openai.new_topic）：exact=["我们换个话题"], fuzzy=["换个话题","聊点别的","换个话题吧"],
transition 文案，enabled 开关——配置装载在票 03 接线，本票只做纯函数 + 表驱动测试。

## Acceptance

- tests/test_new_topic.py：exact 命中（含标点干扰）、fuzzy 距离内命中（含 ASR 误转写
  「换个画图」dist=1）、长句不误触（「我想给你讲个换话题的笑话」「换个话题怎么用英语说」）、
  空词表 / 空文本边界。

## Comments

- 2026-10-27 resolved @ 本票 commit。修正一处测试期望：「换个画图」的编辑距离是 2（话↔画、题↔图）而非扫描时假设的 1，仍在 FUZZY_MAX_DISTANCE=2 容忍内。
