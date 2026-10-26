# 04: 对话记忆 JSONL——写入/滚动/过期清理/注入截取

Type: task
Status: resolved
Blocked by: 01（注入触发读 _force_new_session 标志）

## Body

1. 新模块 `core/services/conversation_memory.py`（纯函数为主 + 轻类持路径）：
   - `append_turn(dir, session_key, user, assistant, now)`：JSONL 追加一行
     `{"ts": ISO, "session_key", "user", "assistant"}`；按天滚动
     `data/memory/<session_key>.<YYYYMMDD>.jsonl`。
   - `purge_expired(dir, retain_days, now)`：惰性清理过期文件（写入时机顺带执行）。
   - `recent_turns(dir, session_key, limit)`：跨日合并取最近 N 轮。
   - `summary_block(turns)`：拼「此前对话摘要：…」注入前缀（纯文本截取，无 LLM 摘要）。
2. 接线（OpenAIManager）：
   - 写入：基类 `_run_transport` 落账成功后新增 `_on_response_complete(text)` 钩子
     （基类 no-op），OpenAIManager 覆写 → memory enabled 时 append_turn + 顺带 purge。
   - 注入：`_build_messages` 在 system 之后、history 之前插入摘要 system 消息——
     触发条件 = `memory.gap_minutes` 内无轮次记录 **或** `_force_new_session` 置位中，
     且文件非空；取最近 `memory.max_turns`（默认 5）轮。注入失败（IO/损坏）降级跳过不阻断。
3. 配置（openai.memory）：`{"enabled": true, "max_turns": 5, "retain_days": 30, "gap_minutes": 30}`；
   目录默认 `<data>/memory/`（RUNTIME 数据卷）；`.gitignore` 排除 `data/memory/`。
4. 隐私边界：记忆只落 NAS 本地，注入片段随常规请求走，不整份外发（spec §隐私边界）。

## Acceptance

- tests/test_conversation_memory.py：追加/滚动命名/过期清理/跨日最近 N 轮/损坏行容错
  （tmp 目录）；test_openai 扩展：写入钩子、触发条件（gap/换话题）、注入消息位置与降级。

## Comments

- 2026-10-27 resolved @ 本票 commit。注入位置实测落点：无主 system prompt 时摘要是首条 system（测试断言按「存在含摘要的 system」而非固定下标）；`data/memory/` 由既有 .gitignore 的 `data/` 覆盖，无需新条目。写入钩子挂 `_store_response` 成功分支——迟到回复（等待方已超时）不写记忆，口径与回填一致。
