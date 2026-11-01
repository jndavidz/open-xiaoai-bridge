# Context

open-xiaoai-bridge 是小爱音箱与外部 AI 服务（小智 AI、OpenClaw、OpenAI 兼容服务、QwenPaw）的桥接器：接管音箱音频输入输出，实现与第三方 AI 的对话。系统架构见 `README.md`（权威），项目结构与模块边界见 `AGENTS.md`。

## Language

**bridge**:
本服务整体。Python 主体 + Rust PyO3 原生模块，Docker 部署于群晖 NAS。
_Avoid_: 网关（OpenClaw 语境下 gateway 另有专指）

**唤醒会话（wakeup_session）**:
`core/wakeup_session.py` 的小智唤醒会话状态机：从唤醒词命中到对话结束的状态管理。
_Avoid_: 会话（泛称）

**连续对话策略**:
`xiaoai_conversation.py`（小爱）/ `openclaw_conversation.py`（OpenClaw，VAD → ASR → Agent → TTS 循环）两种连续对话实现。
_Avoid_: 多轮对话

**双指令表**:
`config.py` 中用户配置的指令映射：同一语音指令在小爱原生/AI 路径间的分发约定。
_Avoid_: 指令路由（路由钩子另指 config 中的代码钩子）

**自触发回环**:
事故档案 `docs/plan/incident-kws-self-trigger-loop.md`（位于上层仓库 docs/plan/）定名的缺陷模式：TTS 播报文案命中 KWS（免唤醒词）词表 + 播放期间 KWS 无闸门，导致音箱无故自我播报。全量播报闸门落点在 `_run_steps` 与 `/api/play/text` 两个入口。
_Avoid_: 死循环

**PCM 推流会话**:
`native/src/tts/pcm_stream.rs`（B-36）的桥侧音乐推流会话：HTTP Range 拉流 → symphonia 解码 → 分块推送到 client PCM 通道；会话状态（pushed_ms/status/duration_ms）经 `pcm_session_state` 暴露给 Python。与 TTS 共用全局 PLAYBACK_TOKEN，新会话自动令旧推流失效。
_Avoid_: spike 会话（spike.rs 已摘除）；播放会话（托管队列会话另指 `core/music/store.py` 的 SessionState）

**互斥编排器**:
`core/music/pcm_orchestrator.py`（B-36）的 TTS/音乐互斥状态机：TTS focus 起播前停推音乐并记忆偏移，focus 归还后 start_ms 续推；带暂停看门狗与推流停滞自愈。对齐原厂 tts-play 的 pause→TTS→play 语义。
_Avoid_: 音频互斥（设备侧无此机制，互斥完全在桥侧）

**playback_backend（双轨）**:
`song_request.playback_backend` 配置：`url`（默认，mediaplayer RPC 投送，2026-11-01 基线）| `pcm`（桥侧推流）。切回 url 即回滚，零代码。
_Avoid_: 播放后端泛称（未指明轨）；DLNA（已收口的另一通道）

**桥侧记账**:
PCM 会话的 position/state 由 bridge 自己回答（actual_start_ms + pushed_ms，经 runtime 发布 `pcm-accounted` 位置快照），不读设备 status——URL 会话 status 恒 3 的盲判在 PCM 轨不存在。
_Avoid_: 位置回读（设备无此能力）
