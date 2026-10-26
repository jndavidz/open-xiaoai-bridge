# 01: X-Session-Key 叠加头 + X-Session-Action one-shot 信令

Type: task
Status: resolved

## Body

OpenAIManager 对接 aurora 会话信令协议（spec §会话头对齐 / §X-Session-Action）：

1. `_headers()`（chat_completions 风格）在现有 `session_header` 机制上**叠加**
   `X-Session-Key: <session_key>`（保留 X-Hermes-Session-Key 兼容；两头并存值相同）。
2. 新增一次性标志 `_force_new_session` + 公开置位接口 `force_new_session()`：
   置位后下一次 `_headers()` 附加 `X-Session-Action: new`，**发送（读取）后立即清位**
   （one-shot）。非 aurora 后端忽略未知头（user story 10）。
3. `reset_session()` 已存在（清本地 `_sessions`），原子性由票 03 的调用侧顺序保证。

## Acceptance

- test_openai 扩展：两头并存断言；one-shot（置位→首次消费→自动清位→第二次无头）；
  anthropic/responses 风格不带会话头（现状不变）。

## Comments

- 2026-10-27 resolved @ 本票 commit。设计修正一处：X-Session-Key 叠加**门控在 session_header 非空**（既有测试揭示「session_header 空 = 纯净请求」契约）；生产 NAS 已配 session_header=X-Session-Key，叠加为幂等回写。注：test_openai 对 OpenAIManager 类属性的跨测试污染是既有现象（全量按字母序 llm_stream 先跑不受影响），单模块组合运行才会暴露。
