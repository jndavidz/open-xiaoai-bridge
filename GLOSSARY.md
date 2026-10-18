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
事故档案 `doc/plan/incident-kws-self-trigger-loop.md`（位于上层仓库 doc/plan/）定名的缺陷模式：TTS 播报文案命中 KWS（免唤醒词）词表 + 播放期间 KWS 无闸门，导致音箱无故自我播报。全量播报闸门落点在 `_run_steps` 与 `/api/play/text` 两个入口。
_Avoid_: 死循环
