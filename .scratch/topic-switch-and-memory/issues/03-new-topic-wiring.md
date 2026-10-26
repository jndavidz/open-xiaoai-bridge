# 03: 暗号接线——模板出口 + OpenAI 控制器原子动作 + transition TTS

Type: task
Status: resolved
Blocked by: 01, 02

## Body

1. `ExternalConversationController._run_one_turn` 模板在 exit_keywords 检查**之后**、
   `_enter_processing` 之前加钩子 `_maybe_handle_new_topic(text) -> bool`（基类默认 False）。
2. `OpenAIConversationController` 覆写：读 `openai.new_topic` 配置（enabled 开关）→
   `match_new_topic` 命中则**同一临界区原子执行**：`backend.reset_session()` +
   `backend.force_new_session()`，随后经 `_play_bypass_text(transition)` 播过渡语
   （不发 LLM，省 2–4s；B-29 步 3 口径——默认裸 play，闸门开启时经 announce），
   返回 True → 模板走 notify/开录收尾，不退对话循环。
3. 根 config.py 与 deploy/config.py 的 openai 段新增 `new_topic` 块（enabled 默认 True，
   词表/transition 按 spec；transition 文案须过词表检查——若命中词表由 announce 拒绝并留
   可见日志，属部署期配置错误）。

## Acceptance

- controller 级测试：命中 → reset+置位被调用 + transition 播放 + 不发 LLM + 返回 continue；
  未命中 → 正常路径不变；enabled=false → 钩子直通。

## Comments

- 2026-10-27 resolved @ 本票 commit。钩子位次在 exit 之后（「退出」优先级高于换话题）；过渡轮 TurnTrace abandon_turn 与确认拦截路径同口径。config 两份（根 + deploy）都加了 new_topic/memory 块。
