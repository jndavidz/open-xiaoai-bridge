# 05: 流式路径不落对话记忆 + 断档时间戳不更新（B-30 真机验收暴露）

Type: task
Status: resolved
Blocked by: —（B-30 票 01–04 已 resolved）

## Problem

现网 `openai.stream=true`，纯文本语音回答走 `OpenAIManager.stream_response_segments`，
其收口（`core/openai.py` finally 块）只调用 `_append_history`，**没有调用
`_on_response_complete`** —— 而 B-30 票 04 的记忆写入与断档时间戳更新都挂在这个钩子上
（非流式路径经 `backend_base._run_transport` 才会触发）。

两个后果：

1. **记忆几乎不落盘**：生产配置下只有被降级为非流式的轮次（如带工具轮）才写 JSONL，
   普通闲聊/问答轮全部丢失 → B-30 的「隔天记得聊过什么」实际不成立。
2. **断档判定长期为真**：`_last_response_at` 同样只在 `_on_response_complete` 里更新，
   流式轮不更新 ⇒ 「距上一轮 > gap_minutes」几乎每轮成立 ⇒ 每轮都注入摘要，
   白烧 token 且可能把旧话题带进新话题。

## Evidence（2026-10-27 真机）

- 10:25–10:27 一段对话：turn=4/7/8 三条流式回复均有完整播报，`data/memory/` 当日文件
  **只多出 1 行**（10:26:25 的 `weather_get` 工具轮——该轮因探测到 tool_calls 被降级为非流式）。
- 10:30:55 turn=11 探针句「我今天让你放过什么歌」答出《晴天》/切了两次下一首/《恒星不忘》，
  这些内容只存在于记忆文件、不在本轮进程历史里 ⇒ **注入路径本身工作正常**，
  坏的只是写入路径（该轮同样未落新行，行数保持 31）。

## Fix

流式收口对齐非流式口径：完整回复入历史的同时，走同一个 `_on_response_complete`
（记忆写入 + `_last_response_at` 更新 + 既有用量/埋点口径不变）。要求：

- 只在 `completed and full_text` 分支触发，异常中断的半截回答仍不写（与 `_append_history` 同判据）。
- 不重复写：非流式路径仍只有 `_run_transport` 一个触发点。
- 记忆写入继续走 executor（AGENTS.md 异步纪律），失败只记日志不阻断。

## Acceptance

- 新增契约测试（prior art：`tests/test_conversation_memory.py`、`tests/test_llm_stream.py`）：
  流式轮完成后记忆文件新增一行且 `user` 为原始文本、断档时间戳被刷新；
  半截中断轮不落盘；非流式轮不重复落盘。
- 全量基线不回归。
- 真机复验：`stream=true` 下连说三轮普通问答，`data/memory/` 行数 +3。

## Comments

- 2026-10-27 由 REBUILD §3 / B-30 语音验收窗口发现并立票；与「记忆 `user` 字段含
  `_with_rule_prompt` 纪律文本」一并处理更省一轮部署（该缺陷见
  `bridge: .scratch/voiceprint-personal-memory/spec.md` §Further Notes）。
- ⚠ 改 `core/openai.py` 与声纹 spec 的档案注入点重叠，动前先查是否有他方会话在改同一文件。
- 2026-10-28 代码落地（两缺陷一并修，同批改 `core/backend_base.py` /
  `external_conversation.py` / `openai.py`）：
  ① 流式收口 `finally` 在 `completed and full_text` 分支补调 `_on_response_complete`
  （记忆写入 + `_last_response_at` 刷新；基类钩子是 no-op，非流式口径不变）；
  工具回环探测轮 `completed=False` 不触发，写入仍由 `_run_transport` 独占不双写。
  ② 原始文本分离：`backend_base` 新增 `note_raw_turn_text`（控制器发请求前登记
  原始 ASR 文本、收尾 finally 清除）+ `_record_user_text`（startswith 守卫回落）；
  `_append_history` / `_on_response_complete` / `logger.user_speech` 三个落账点
  统一经它取原始值——skill/send 直发路径未登记，行为逐字不变。
  契约测试 7 项入 `tests/test_llm_stream.py`（`StreamingMemoryWritebackTest` +
  `RawTurnTextSeparationTest`）；全量 755 passed / 3 skipped / 9 failed
  （9 项均为 WSL 缺 numpy 的既有环境失败，经 HEAD worktree 对照判定与本批无关）。
  **剩余：镜像重建 + recreate + 真机复验（验收 §最后一条）后转 resolved。**
- 2026-10-28 部署完成：WSL 路径 A 构建（HEAD `6f5a457`）→ SSH 流式 load（NAS 镜像
  `bd2301c1`，旧镜像自动摘 tag 可回滚）→ `deploy/config.py` 同步 NAS（备份
  `config.py.bak-20261028-presync`，`COMMAND_ALIASES` 接线随合流上生产，空表=零行为）
  → force-recreate。容器内验证：票 05 两处修复符号在镜像（backend_base/openai 各 3 处）、
  暂停标记修复在镜像；KWS 启动日志 `[score:2.5, threshold:0.3]`；`/api/health` healthy。
  **剩余仅真机复验：`stream=true` 连说三轮普通问答，`data/memory/` 行数 +3 且 `user`
  无纪律文本 → 转 resolved。**
- 2026-10-28 真机复验通过（收口）：`data/memory/agent_javis_home.20261028.jsonl` 当日
  **53 行**（`15:10` 仍在追加），抽查首行 `user` 为原话「播放整张叶惠美的专辑」、
  `assistant` 为回复正文——**无纪律 prompt 回灌**；`stream=true` 为常态，工具轮与普通轮
  均在写入。验收两条中「turntrace 出现当日 JSONL」当场暴露新 bug（persist 落 `/app/core/data`
  镜像层），已由 `bridge 48c5372` 修正为 `/app/data/turntrace` 并把当日 24 行抢救回卷
  （见 B-32 行 / 台账 §5）。本票转 **resolved**。
