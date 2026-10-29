# AGENTS.md

> 小爱音箱与外部 AI 服务（小智 AI、OpenClaw）的桥接器。
> 接管音箱音频输入输出，实现与第三方 AI 的对话。

## 系统架构

详见 [README.md 系统架构](README.md#系统架构)（含 Mermaid 流程图和各模块工作流程）。

## 项目结构

```
open-xiaoai-bridge/
├── main.py                        # 入口：解析环境变量，启动 MainApp
├── config.py                      # 用户配置（唤醒词、路由钩子、TTS、OpenClaw 等；生产版双指令表见 deploy/config.py）
├── core/
│   ├── app.py                     # MainApp 主控制器（单例，管理生命周期）
│   ├── xiaoai.py                  # XiaoAI 设备接入 / 事件桥接
│   ├── xiaoai_conversation.py     # 小爱连续对话策略
│   ├── xiaozhi.py                 # 小智 AI WebSocket 协议客户端
│   ├── openclaw.py                # OpenClaw 网关客户端（连接、消息、TTS 播放）
│   ├── external_conversation.py   # 外部后端连续对话循环主体（VAD/原生 ASR → 后端 → 逐段流式 TTS；P1 ticket 02 起带管线态与取消检查点，04 起边生成边播）
│   ├── openclaw_conversation.py   # OpenClaw 对话控制器（薄封装，继承 external_conversation）
│   ├── openai_conversation.py     # OpenAI 对话控制器（薄封装，同上）
│   ├── qwenpaw_conversation.py    # QwenPaw 对话控制器（薄封装，同上）
│   ├── backend_base.py            # 外部后端管理器公共骨架（BackendManagerBase，C1）
│   ├── backend_registry.py        # 后端注册表（C1/P0 票 07）
│   ├── wakeup_session.py          # 小智唤醒会话状态机
│   ├── ref.py                     # 全局引用注册表（get/set 依赖注入）
│   ├── device/                    # 类型化设备能力层（registry/client/results/transport/playback_events）
│   ├── tools/                     # Agent 工具层：registry 三函数 + 只读工具（hass/weather/music/device_status）+ guardrails 四分级护栏 + ir_send C 级工具（P3）
│   ├── commands/                  # 指令层深模块（C4/P2 票 01）：双表执行器 run_steps + HA client + 动作构造器 + 唤醒边界路由 route_before_wakeup + 词表对账 audit（P2 票 02）+ 文本归一化/别名键 matching（B-31）
│   ├── music/                     # 托管播放（B-04…B-06，默认不接管生产流量）：TrackRef/队列项/位置快照 + ManagedStore（SQLite 单事务）+ ManagedExecutor（有界单 worker）+ EventBus/ManagedService（幂等回执、代次闸门、事件同事务）+ auth（music-control 专用凭据）+ player/backends（EOF 判定与 DLNA/RPC 后端，主动 Stop 后推进一次）+ tts_focus/tts_providers（TTS focus、恢复资格与四 provider 独立结果）
│   ├── models/                    # 模型文件（KWS/VAD/ASR，.gitignore 排除）
│   ├── assets/sounds/             # 音效（tts_notify.mp3 等）
│   ├── services/
│   │   ├── speaker.py             # SpeakerManager 音箱硬件控制（含 mic_gate 播报闭麦/恢复快照）
│   │   ├── api_server.py          # HTTP REST API（鉴权/注入/健康；含 /api/audio_input 恢复通道）
│   │   ├── auth.py                # 业务 API Bearer 鉴权（纯函数，失败关闭）
│   │   ├── health.py              # /api/health 纯函数组装（内存态 + 可选设备探针）
│   │   ├── inject_control.py      # PC 注入开关判定 + 原子开窗/回滚（纯逻辑）
│   │   ├── admin_api.py           # 后台面板 API（/admin 页面 + /api/admin/*）
│   │   ├── managed_api.py         # 托管播放 HTTP/SSE（B-05）：/api/music/*，music-control 凭据 + target 约束，失败关闭
│   │   ├── admin_static/index.html# 面板单页前端（零外部依赖）
│   │   ├── audio/
│   │   │   ├── stream.py          # GlobalStream 全局音频流（多路输入广播）
│   │   │   ├── codec.py           # 音频编解码
│   │   │   ├── channels.py        # 六麦帧对齐/选麦/下混（纯 stdlib，ticket 04）
│   │   │   ├── watchdog.py        # 录音通道无帧看门狗状态机（ticket 04）
│   │   │   ├── playback_timing.py # 播放时长/完成等待纯函数原语（B-29 步 1：pcm_duration/sleep_playback_done/文本预算估算）
│   │   │   ├── speaking_channel.py# 对话循环发声接缝（B-29 步 2）：播放 API 只经 open() 产出 session（进门即物理断录）
│   │   │   ├── vad/silero.py      # Silero VAD 语音活动检测（ONNX）
│   │   │   ├── kws/sherpa.py      # Sherpa KWS 关键词唤醒（kws/__init__ 含 listen_disabled 持久开关 + 播放闸门）
│   │   │   └── asr/sherpa.py      # Sherpa ASR 离线语音识别（SenseVoice）
│   │   ├── conversation_memory.py  # 对话记忆 JSONL（B-30 票 04）：按天滚动/过期清理/注入摘要；data/memory/ 本地卷
│   │   ├── led_language.py         # LED 灯语（P3 票 02）：管线态四态可视化，led_language.enabled 默认关
│   │   ├── tts/router.py           # 统一 TTS provider 路由与播放
│   │   ├── tts/doubao.py          # 豆包 TTS 客户端（火山引擎）
│   │   ├── tts/openai.py           # OpenAI-compatible TTS 协议客户端
│   │   ├── tts/mlx_audio.py        # MLX-Audio TTS provider（复用 OpenAI 协议客户端）
│   │   └── protocols/
│   │       ├── websocket_protocol.py  # 小智 WebSocket 协议实现
│   │       └── typing.py              # 协议类型定义
│   └── utils/
│       ├── logger.py              # 彩色日志（XiaozhiLogger 单例；含内存缓冲 handler）
│       ├── log_buffer.py          # 内存环形日志缓冲（面板增量拉取）
│       ├── runtime_overrides.py   # 运行时配置覆盖层（data/runtime-overrides.json）
│       ├── config.py              # ConfigManager（嵌套路径查询、热重载、覆盖层合并）
│       ├── config_loader.py       # config.py 动态导入
│       ├── turn_trace.py          # 每轮延迟埋点 [TurnTrace]（默认关；纯数值无 PII；P1 ticket 02 起轮次号与 pipeline 同源；P2 ticket 03 起带用量字段并喂 usage_ledger）
│       ├── usage_ledger.py        # 用量记账台账（P2 票 03）：每轮一行 JSONL 落盘 data/usage/，只记账零控制；面板「用量」页数据源
│       ├── pipeline.py            # 管线态/轮次标识/取消传播骨架（P1 ticket 02；IDLE/LISTENING/PROCESSING/SPEAKING + TurnCancelledError）
│       ├── sentence_split.py      # 句读切分器纯函数（P1 ticket 03：强标点≥4/弱≥12/≥20 强切；流式增量→句段，04 起逐段直喂 TTS）
│       ├── new_topic.py           # 「换个话题」暗号识别纯函数（B-30 票 02：exact 整句 + fuzzy ≤8 字 dist≤2；长句永不进模糊通道）
│       ├── base.py                # 基础工具
│       └── file.py                # 文件工具
├── native/                        # Rust PyO3 扩展（maturin 编译）
│   └── src/
│       ├── lib.rs                 # 模块入口：on_output_data, start_server, stop/start_recording, stop/start_playing
│       ├── server.rs              # WebSocket 音频服务器（TCP :4399）
│       ├── python.rs              # Python 回调注册中心（HashMap）
│       ├── macros.rs              # 辅助宏
│       └── tts/                   # TTS 音频处理（流式、PCM 直通、MP3 解码）
├── app/openclaw/                  # OpenClaw 设备身份存储（Ed25519 密钥）
├── skills/xiaoai-tts/             # Agent 工具：通过 HTTP API 控制小爱播放
└── tests/                         # 测试脚本
```

## 核心组件

### MainApp (core/app.py)

应用主控制器，单例模式，管理全部服务生命周期。

- `instance(enable_xiaozhi, enable_openclaw)` → 单例获取
- `run(enable_api_server)` → 启动各服务
- `set_device_state(state)` → 管理设备状态（IDLE / LISTENING / SPEAKING / CONNECTING）
- `send_text(text)` → 发送文本到小智
- `send_to(backend, text, *, wait_response, and_play)` → **向注册表后端发送的单一入口（B-28 票 03）**：新后端不再需要新增门面方法；`and_play=True` 播报回复（追加 rule_prompt），False = Agent 自主播报（追加 rule_prompt_for_skill）
- `set_session_key(backend, session_key)` → 运行时切换任意注册表后端的会话键
- `send_to_<name>(...)` / `set_<name>_session_key(...)` → 兼容委托（deprecation 期，调用面迁移完即删）
- `schedule(callback)` → 主线程任务队列
- `shutdown()` → 优雅关闭

**边界约束**:
- `MainApp` 是业务主循环和设备状态的单一入口
- `device_state` 以 `MainApp` 为准，其他模块通过代理回写，不各自维护平行状态
- `MainApp.loop` 是业务协程的主调度循环

### XiaoAI (core/xiaoai.py)

小爱音箱交互接口，类级变量（classmethod 风格）。

- `init_xiaoai()` → 初始化原生服务，注册事件处理
- `on_event(event)` → 处理小爱事件（RecognizeResult / AudioPlayer）
- `on_input_data(data)` / `on_output_data(data)` → 麦克风 / 扬声器音频回调
- `run_shell(script, timeout)` → 远端 shell 执行
- 内部维护独立 `async_loop`（后台线程），仅用于原生扩展回调和事件桥接

**边界约束**:
- 负责设备接入和事件桥接，不承载连续对话策略
- 连续对话状态放在 `xiaoai_conversation.py`
- `async_loop` 不应承载新的业务状态机

### XiaoZhi (core/xiaozhi.py)

小智 AI WebSocket 协议客户端，单例模式。

- `connect()` / `disconnect()` → 连接管理
- `send_audio(frames)` / `send_text(text)` → 发送音频 / 文本
- `send_start_listening(mode)` / `send_abort_speaking(reason)` → 协议命令
- 回调委托：`on_incoming_audio`, `on_incoming_json`, `on_network_error` 等

**边界约束**:
- 只负责协议收发，不负责唤醒策略和连续对话策略
- `session_id` 必须由服务端消息更新，不能长期使用空值发送控制消息

### OpenClawManager (core/openclaw.py)

OpenClaw 网关客户端，管理 WebSocket 连接、消息分发、自动重连、TTS 播放。

- `initialize_from_config(enabled)` → 从 config 初始化
- `connect()` → 建立连接（Ed25519 设备身份认证）
- `send(text, wait_response)` → 发送消息，返回 run_id 或回复文本，失败返回 None
- `send_and_play(text, wait_response)` → 发送并 TTS 播放回复
- `is_connected()` / `is_enabled()` → 状态查询

**内部机制**:
- Ed25519 设备身份认证（密钥存储在 `app/openclaw/identity/`）
- WebSocket ping/pong + tick 事件监控连接健康
- 指数退避重连（初始 1s，最大 60s）
- 请求 ID 映射 `_pending: dict[str, asyncio.Future]` 追踪响应
- TTS 播放：通过共享 TTS Router 选择 `xiaoai`、`doubao`、`openai` 或 `mlx_audio` provider；缺省时保持 `tts_speaker` 的旧选择逻辑
- Rust TTS 播放使用单一活动 `playback_token`：开始新的 Rust TTS 会使旧 token 失效；`stop_tts_playback(token)` 只应由持有该 token 的调用方定向停止自己的播放

**连接参数限制**:
- `client.id`: 必须是 OpenClaw 预定义常量
- `client.mode`: 必须是预定义常量
- `session_key`: 只从 config.py 读取

### OpenClawConversationController (core/external_conversation.py)

外部后端连续对话控制器（主体在 `ExternalConversationController`，OpenClaw/OpenAI/QwenPaw 三个控制器是薄封装子类）。唤醒词触发后进入独立的 VAD/原生 ASR → 后端 → TTS 循环。

- `start()` → 进入对话模式
- `stop()` → 退出对话
- `is_active()` → 状态查询

**对话循环** (`_run_one_turn` 轮模板，B-28 票 03 起单点化——旧 `_run_one_turn_with_local_asr` / `_run_one_turn_with_xiaoai_asr` 双写已收敛；输入链路差异只在 `_acquire_user_text()` 策略点):
1. 取文本：VAD 检测语音开始（`_wait_for_speech`）+ SherpaASR 离线识别 / 等待原生 ASR 定稿（`_wait_for_xiaoai_asr_text`）
2. 取消检查点①（ASR 定稿后）
3. C/B 级应答拦截 → 退出关键词检测
4. 发送到后端（`_speak_response_segments`，P1 ticket 04：pump 协程驱动句段流，
   LLM 请求在「咻」提示音期间即上路）
5. 逐段流式 TTS：首段到达即断录起播，后续段到达即接力播放（每段过
   stream_segment 检查点；播放令牌整轮一个；非流式后端 = 单句序列 =
   单段一次播放，行为与流式化之前逐字等价）
6. 恢复监听（notify → 开录 → VAD 链路等静音）

**管线态与取消传播（P1 ticket 02，流式化地基）**:
- 每轮对话经 `core/utils/pipeline.py` 分配轮次号并按管线态迁移
  （idle → listening → processing → speaking → idle），`[Pipeline] turn=N` 日志行
  与 `[TurnTrace] turn=N` 埋点同号；管线态与 MainApp 协议态**分层并存**，互不驱动。
- 固定异步检查点：**ASR 定稿后 / LLM 请求前 / 播放等待内（起播前+返回后）/
  流循环内每个句段（stream_segment，ticket 04 起为逐段播放主检查点）**；
  本轮已取消（`pipeline.cancel_*`，由 `on_interrupt`/`reset_all_sessions` 置位）即抛
  `TurnCancelledError` 静默退出——不发请求、不播后续音频、不打 ERROR。
  事件线程侧（`consume_xiaoai_recognize_result`）用非抛式 `turn.check()` 吞定稿文本。
- 打断两层机制不变：置取消（新增面）+ `controller.stop`/`task.cancel`/播放令牌
  定向杀流（既有面）。

**回声防护机制**:
- `stop_recording` → kill 远端 arecord → 麦克风物理静音
- TTS 和提示音都在关麦期间播放，开麦后 VAD 从干净状态开始检测
- `VAD.resume()` 会自动 `_reset_state()` + `input_bytes.clear()`，清除旧的 `speech_frames` 和音频流缓冲

**VAD 状态泄漏陷阱**:
- VAD 检测循环持续运行，`speech_frames` 会不断积累音频帧
- 如果 `resume()` 不调用 `_reset_state()`，旧帧（唤醒词回声、TTS 回声）会泄漏到下一轮检测，导致 ASR 识别出幽灵音频
- `pause()` 会调 `_reset_state()`，但 `resume()` 必须也调——两者都需要清理状态

**边界约束**:
- 使用独立 VAD Future，不与 WakeupSessionManager 冲突
- TTS 完全阻塞，播放完成后才继续监听
- 自己持有并管理当前 TTS 的 `playback_token`；停止 OpenClaw 对话时应调用 `stop_tts_playback(token)`，不要在外层直接无 token 全局停止 Rust TTS
- **对话循环内发声一律经 `self.speaking_channel.open()` 产出的 session**（B-29 步 2）：`play_pcm`/`play_segment_tts` 只在 session 上，open 进门即物理断录——「发声前必须断录」由结构强制；旁路文本播报走 `_play_bypass_text`（`conversation.bypass_play_via_announce` 门控，默认裸 play），新发声点禁止绕过这两条路

### WakeupSessionManager (core/wakeup_session.py)

小智唤醒会话状态机，协调 KWS → VAD → 小智/OpenClaw 的唤醒流程。

- `wakeup(text, source)` → 处理唤醒（调用 `before_wakeup` 钩子，路由到 XiaoZhi 或 OpenClaw）
- `wait_next_step(timeout)` → 异步等待状态变化（带待决状态缓冲）
- `update_step(step, step_data)` → 更新步骤
- 事件回调：`on_interrupt()`, `on_wakeup()`, `on_tts_start()`, `on_tts_end()`, `on_speech()`, `on_silence()`
- `on_interrupt()` → 小爱唤醒时：cancel OpenClaw task、停止设备音频播放、恢复录音通道、stop XiaoAI conversation

**路由规则**（`before_wakeup` 返回值）:
- `"xiaozhi"` → 走小智流程
- `"openclaw"` → 走 OpenClaw 连续对话
- `None` → 不处理（用户自行处理）

**边界约束**:
- 它是"小智唤醒会话状态机"，不是通用事件总线
- 只允许缓存 `on_speech` / `on_silence` 等外部探测信号
- 不要缓存 `on_wakeup` / `on_interrupt` 等控制步骤

### XiaoAIConversationController (core/xiaoai_conversation.py)

小爱自身的连续对话管理。

- `handle_text_command(text, speaker)` → 处理退出 / 连续对话关键词
- `handle_listening_timeout(speaker)` → 超时重试逻辑
- `handle_audio_player_instruction(header_name)` → 检测播放器指令退出
- `handle_playing_status(playing_status, speaker)` → TTS 完成后重新唤醒

**边界约束**:
- 小爱连续对话和小智唤醒 / 会话超时是两套独立机制
- 只有在"小爱连续对话确实激活"时才允许停止
- 小智超时退出时不应打印"小爱停止连续对话"日志

### SpeakerManager (core/services/speaker.py)

音箱硬件控制。

- `play(text, url, buffer, blocking, timeout)` → 播放文字 / URL / PCM 缓冲
- `stop_device_audio()` → 停止设备上的播放链路（阻塞 TTS / 非阻塞 TTS / PCM），并重启 PCM 播放通道
- `next_track()` / `prev_track()` → 切设备自身播放队列（能力层 `playback_next`/`playback_prev`；桥内点歌队列归 `song_request`）
- `wake_up(awake, silent)` → 唤醒 / 休眠小爱
- `abort_xiaoai()` → 中断小爱当前操作
- `ask_xiaoai(text, silent)` → 让小爱执行指令
- `run_shell(command, timeout)` → RPC shell
- `mic_gate(reason, max_seconds)` → 播报期间固件闭麦；退出恢复进入前麦态快照（绝无条件开麦）；持有期间串行化，避免并发播报嵌套
- `announce(text, *, reason, exempt, wait, gate_seconds, wait_budget_ms)` → **播报编排深模块（P0 ticket 02/03）**：进入做双闸门 + 词表安全检查（文案命中唤醒/指令词表默认拒绝并告警，raise `AnnounceRejected`；`exempt=True` 显式豁免），body 经设备能力层播放，退出等完成（事件适配 + 文本估算超时兜底）并 finally 双闸门归还。**全部播报入口（/api/play/{text,url,file} 与 `_run_steps`）已经此接入——闸门手工编排的权威位置只有这一个**，新增播报入口（早间简报等）一律一行接入
- `reconcile_mic_state()` → 首个事件到达后清理 bridge 崩溃遗留的设备级静音
  > ⚠️ KWS `listen_disabled` 当前是内存态：bridge 重启后无法区分「用户停止聆听」与
  > 「崩溃遗留静音」，reconcile 会按未停止聆听处理。需要严格区分时须持久化该开关（未实现）。

> ✅ **`set_mic(on=...)` 语义已修正（ticket 03，2026-10-24）**：
> `on=True` 发 `pnshelper event_notify {src:3,event:8}` 开麦；`on=False` 发 `event:7` 闭麦。
> `event:8` 在麦已开时返回 `code:-1` 属幂等语义，成败以 `get_mic()` 回读为准。
> 设备行为依据：`/tmp/mipns/mute` 存在即静音；`event:7` 创建、`event:8` 删除。
> 设备事实见 `ws: docs/devices/lx06/pnshelper-event-space.md` §3.1/§3.5；
> 播报期固件闭麦闸门见 [`docs/plan/README.md`](docs/plan/README.md) **B-02**。
**边界约束**:
- `stop_device_audio()` 只负责"停播放"，不负责恢复录音；`start_recording()` 属于会话层恢复逻辑，应由 `WakeupSessionManager` / `OpenClawConversationController` 等上层按场景决定

#### ⭐ ASR 事件的真实来源（源码级确认，2026-10-19）

> 取证：`D:\_work\dev\reverse\lx06-firmware-reverse\run\client-monitor-reverse.md`
> 上游源码：`coderzc/open-xiaoai` `packages/client-rust/src/services/monitor/file.rs`

**`xiaoai_asr` 的 ASR 文本不经任何 IPC，是 client 轮询设备 tmpfs 上的日志文件**：

```
client（设备侧，Rust + tokio）
  └─ loop { read_line; if EOF { sleep(10ms) } }     ← 10ms 轮询，无 inotify
       ├─ /tmp/mico_aivs_lab/instruction.log         ← ASR 结果（原文透传，不做 JSON 解析）
       └─ /tmp/open-xiaoai/kws.log                   ← 唤醒词
  └─ arecord 子进程 → PCM 16k                        ← 音频（不是 ALSA API）
```

**`file.rs` 的关键三行语义**：

```rust
let mut position = metadata.len();   // ★ 启动时跳到末尾 ⇒ 不重放历史
if current_size < position { position = 0; }      // 检测截断/重建
sleep(Duration::from_millis(10)).await;           // 轮询间隔
```

**排障对照表**：

| 症状 | 先查什么 | 不要查 |
|---|---|---|
| ASR 偶发丢结果 | ① **monitor 是否重启过**（每次 WS 重连 `start()` 都会 abort 旧 task，新 task 从文件末尾起 ⇒ 重启窗口内的 ASR 永久丢失）；② 设备上 `instruction.log` 是否被截断 | ❌ 网络 RTT / token / 后端 / 模型参数 |
| 唤醒词丢事件 | `/tmp/open-xiaoai/kws.log` 同上 | ❌ KWS 模型参数 |
| 解析失败 | ✅ 已排除：client **不做 JSON 解析**（`NewLine(String)` 原文透传），bridge 侧用 `.get()` 容错 | ❌ 别再怀疑格式 |

**三个坑**：

0. **LX06 硬件是 6 麦，不是 8 麦**（真机逐通道实测：ch0~ch5 有信号，**ch6/ch7 恒零**；
   `hw:0,3` 的 8ch 只是音频格式）。6 路底噪接近（−59~−55 dBFS）⇒ 环形阵列等权、
   无明显主通道 ⇒ 不要指望「取单通道最优」能提升识别率。
   详见 [`docs/plan/asr-input-reliability.md`](docs/plan/asr-input-reliability.md) §1.1；待办见 [`docs/plan/README.md`](docs/plan/README.md) B-03


1. **`arecord` 抢 ALSA 设备**：client 用 `arecord --quiet -t raw -D -f -r -c …` 抓麦克风，
   与 bridge 的 `arecord -D noop` 采集**不能同时在 client 级并发** ——
   这是 `hw:0,3` → `noop` patch 的另一面（`dsnoop` 让 1ch 与 6ch 可同时**打开**，
   但两次独立 `arecord` 进程仍可能互抢）。
1.5 🔴 **自己调 `arecord` 必须加 `-t raw`**：实测不加时 `-d 1` 产出 **32,044 字节**
   （预期 32,000）⇒ 多出 **44 字节 WAVE 头**，会把 ASR 的 PCM 流整体打歪。
   这是「低成本高杀伤」的坑（client 源码里有 `-t raw`，所以它没问题）。
2. **延迟下限 = 10 ms 轮询**（不是网络）。调 `xiaoai_asr` 响应延迟先看这个。

3. 🔴 **`InjectRelay` 的 WS 文本订阅曾整窗收不到**（bridge 侧第二根因，与上表
   「monitor 重启」并列）：`publish_text()` 只做实时广播，而 `text_queues` 的订阅者是
   **WS 连接时才创建** ⇒ 订阅晚于 ASR 结果产生时，那段文本永久丢失。
   判据：bridge 日志 `[InjectRelay] texts=N`（N>0）但 WS 客户端什么都没收到。
   ✅ **已修（2026-10-21）**：改为 `seq` + `?since=N` 断点续取 + 有界回放（`MAX_TEXT_LOG=200`），
   且**先回放后订阅实时队列**（结构性消除顺序陷阱）。
   ⚠️ **同一轮实测**：一句话会产生**多条** `RecognizeResult`（累积快照），`is_final` 只在最后一条
   ⇒ 帧里必须带 `is_final`，客户端要区分「预览/提交」并**覆盖**而非追加。
   ⚠️ **不要**用「窗口期 buffer + `open_session` 清空」的方案：清空时机与新窗口绑定，
   会误杀「窗口结束后才连 WS」这个正常场景（详见
   [`docs/plan/inject-relay-text-buffer-replay.md`](docs/plan/inject-relay-text-buffer-replay.md) §5）。

**🚫 死通道：`kws.log`（别被 `kws.rs` 误导）**

`/tmp/open-xiaoai/` 目录**根本不存在**（实测）⇒ client 的 `kws_monitor` 永远卡在
`while !exists { sleep(10ms) }` 空转，不产生事件、无副作用。

- 唯一写入方是 `examples/kws` 那套**独立方案**的 `monitor` 二进制，**当前未安装**；
- bridge 的唤醒走**自己的 sherpa-onnx**（`core/services/audio/kws/sherpa.py`，消费 WS 的 PCM）；
- `core/xiaoai.py` 只分支 `instruction` 与 `playing`，**从不处理 `kws` 事件**。

⇒ 两条技术路线二选一，当前选 bridge 侧。**读 `kws.rs` 会以为「唤醒从这来」，实际没走过。**
⚠ 若将来启用，先修 `kws.rs` 的 `data[0].parse().unwrap()` + `data[1]` 直接索引
（一行坏数据即 panic，tokio task 不会自动重启）。

**🔴 排障前置判据：先确认 4399 是 `ESTABLISHED`**

实测 `SYN_SENT`（持续重连）时：client 活着，但连不上 NAS 的 bridge
⇒ 没有音频 ⇒ 没有 ASR ⇒ `instruction.log` 不再增长（实测 1 小时无新写入）。
**没有这条连接，一切下游现象都无意义**，否则会把网络故障误判成 ASR 缺陷。

**⚠ 已知缺陷（不改，但要知道）**：`position = metadata.len()` 让 client 启动时跳过历史
⇒ **monitor 重启 = 丢那段时间的 ASR**。改成 `position = 0` 会重放全部历史（可能重复执行指令），
**当前取舍更安全，不建议改**。排障时把它当成「已知丢事件窗口」，别误判为后端故障。

**🔴 另一个已知缺陷：`Speaker.play(blocking=True)` 的 TTS 文本未做 JSON 转义**

`core/services/speaker.py:75` 只做了 shell 单引号转义，而 `tts_play.sh` 内部把 `$1`
**直接插进 `{"text":"$1"}`**（无 JSON 转义）⇒ 文本含 `"` / `\` / 换行时 **JSON 破裂**：
ubus 解析失败（`exit 1`）或文本被静默截断。

- `blocking=False` 分支**安全**（走 `json_encode` = `json.dumps`）；
- 因播报文案多为中文短句，**该缺陷一直未暴露**——属潜伏；
- 完整取证与修法（**注意：必须先 JSON 转义、后 shell 转义，顺序反了会被破坏**）见
  [`../docs/devices/lx06/tts-text-escaping-defect.md`](../docs/devices/lx06/tts-text-escaping-defect.md)；待办见 [`docs/plan/README.md`](docs/plan/README.md) B-04。

> 方法论：Rust 二进制去符号后反编译（2104 个匿名 `poll` 状态机）性价比很低。
> **先 `git clone` 找上游源码**是正确顺序——本轮 5 个文件就拿到了比逆向更确定的答案。

#### ⚠️ 设备能力扩展面（逆向已解出，但 bridge **尚未接入**）

> 来源：固件逆向任务 `D:\_work\dev\reverse\lx06-firmware-reverse\run\open-xiaoai-integration-map.md`
>
> **精确盘点**：`grep -rhoE "ubus call [a-z_]+ [a-z_0-9]+" --include=*.py .` ⇒ bridge **只用 4 个** ubus 调用
> （`pnshelper event_notify` / `mibrain text_to_speech` / `mibrain ai_service` / `mediaplayer player_play_url`），
> 而设备已解出 **258 个 ubus 方法、138 条 AIVS 指令、`mediaplayer` 43 个方法**。**利用率 ≈2%。**

| 优先级 | 能力 | ubus | 验证状态 | 备注 |
|---|---|---|---|---|
| **1** | **红外发射**（控空调/电视/机顶盒） | `ir_agent play '{"async":0,"freq":38000,"code":"<base64>","head":""}'` | ✅ **真机已验**；🟢 **P3 票 04 已收编**（B-27：`ir_send` 能力行 sysfs 直写 + C 级工具；网络前置复验过——NAS→LX06 ssh echo + P0.5 常驻连接 ESTABLISHED，旧「跨机不可达」记录 2026-10-20 已自愈；语音端到端已真机验证——2026-10-26：说出指令 → 复述确认 → 应答 → 发射，内核 `[ir_tx] send finished` 留证；真实设备码值待用户配，台账 `ws: docs/plan/pending-followups.md` §1） | 全新维度；固件内**无本地码库**，码值需外部提供（`ir.codes` 配置表）。<br>bridge 走的是 sysfs 直写：`echo "<µs序列>" > /sys/ir_tx_gpio/ir_data`（**不需 daemon、不需 heatshrink**）。<br>内核语义已解出：**偶数索引=载波（`val/26.4` 周期）、奇数索引=静默** ⇒ 任意 IR 协议可直接由规格生成，**无需码值库**。⚠ 发送期间关中断 ⇒ 须串行化、限长、不与播放/TTS 并发（均已落：串行锁 + 512 项/300ms 限长 + speaking 态拒绝）。详见 [`../docs/devices/lx06/kernel-surface.md`](../docs/devices/lx06/kernel-surface.md) §4 |
| **2** | 音量直控 | `mediaplayer player_set_volume '{"volume":35}'` | ✅ **真机已验** | 现走 `ai_crontab`/`ask_xiaoai` 绕路，可直接调。**43 个方法与参数类型全表见[`../docs/devices/lx06/mediaplayer-api.md`](../docs/devices/lx06/mediaplayer-api.md)** |
| **3** | 播放控制（上一首/进度/循环） | `media_control` / `player_set_positon` / `player_set_loop`；上一首/下一首走 `mphelper iot_next` / `iot_prev` | ✅ 上一首/下一首已入表（B-35，`playback_next/prev`，脚本层判据核对、**真机待验**）；进度/循环 ⚠️ 静态解出，且 **`player_set_loop` 首版禁止**（B-02：通用 shell 闸门拒绝 + 未登记能力，缺 `media` 会打崩 mediaplayer） | ⚠️ **`code:0` ≠ 播放成功**；DLNA 投送态队列归投送方，设备 next 无效 |
| **4** | 离线本地指令（断网可用） | `ai_crontab new` | ✅ 真机已验 | ⚠️ ubus 是 **root-only**；`duration` **实测无效** |
| **5** | 闹钟/提醒（断电持久） | `alarm` 25 方法 | 部分已验 | ⚠️ `chime_hourly_set` 的 `OFF` 返回 `code:0` 但**不写** |
| **6** | ⭐ **播放完成回调**（免轮询） | `ubus listen` → `play_status_broadcast_event`（`status:1` 开始 / `0` 结束） | ✅ **2026-10-20 真机已验** | 🔴 **推翻** `api_server.py:247`「无完成回调」的注释。⚠️ **不带 id ⇒ 并发不可用（只适合串行）**；⚠️ **HTTP 源不可达时只发 `status:1` ⇒ 必须保留超时兜底**。详见 [`../docs/devices/lx06/ubus-events-play-status.md`](../docs/devices/lx06/ubus-events-play-status.md) |
| **7** | 本地音频直放（零网络） | `qplayer <本地文件>` | ✅ 2026-10-20 实测（opus/wav，ALSA 直通） | 同步阻塞=音频时长；**不产生广播事件**；⚠️ **任何参数都被当文件名**（无 help）⇒ 勿传外部输入 |

> 🔴 **音量回读的口径坑（2026-10-20）**：`player_set_volume` 用**档位**，
> 而 `get_media_volume` 返回**百分比**（设 6→读 25，设 8→读 40），且 `amixer` 恒定 85% 完全无关。
> ⇒ **`set` 之后必须用 `player_get_context` 回读**，不能用 `get_media_volume` 校验。
> 详见 [`../docs/devices/lx06/mediaplayer-api.md`](../docs/devices/lx06/mediaplayer-api.md) §2.4。
>
> 🔴 **内存红线与存活检测（2026-10-20 真机事故）**：设备**只有 118 MB 内存、无 swap**，
> 常态 free 仅 2.8–5.9 MB ⇒ 内存临界时 `mediaplayer` 会反复崩，表现为
> `ubus list` 里对象消失 + 调用返回 **`code:-1`**。
> 🔴 **`respawn.retry=0` 在 procd 里意为「不设重启上限」= 无限重启，不是「不重启」**
> （实测 `kill -9` 后 t+5 s 自动拉起）⇒ **会自愈**，缺的是「发现」不是「拉起」。
> 同样 `retry:0` 的还有 `mico_aivs_lab`/`mibrain_service`/`pns_ubus_helper`（均会自动拉起）。
> ⇒ bridge **应做**：①播报串行 ②存活检测（`service list {"name":"mediaplayer"}` 判 `running`）
> ③轮询 `system info` 的 `memory.free`，**free<10 MB 时降级播报并发** ④收到 `code:-1` 立刻查存活。
> ⚠️ **不要手动 `/etc/init.d/mediaplayer start`**（procd 已会自愈；只在确认 procd 未拉起时才用）。
> **当前 bridge 没有存活检测。** 详见 [`../docs/devices/lx06/mediaplayer-api.md`](../docs/devices/lx06/mediaplayer-api.md) §2.6；
> 待办见 [`docs/plan/README.md`](docs/plan/README.md) **B-07**。
>
> 🔴 **复位纪律（2026-10-20 故障复盘，见逆向侧 `run/fault-postmortem-2026-10-20.md`）**：
> 本轮故障是**断电上电才彻底消失**的 ⇒ 排障时**优先建议用户断电上电**（最彻底、最省时），
> 不要反复 ubus 试探延长故障窗口。
> ⚠️ 订正：初版曾写"本设备 reboot 走 kexec 不清驱动"——已证伪（本机 `reboot_mode=cold_boot`、
> 无 kexec、内核无 KEXEC，软重启本应走冷启动）。那次"软重启后仍在"更可能是该软重启
> **根本没真正发生**（uptime 矛盾）。故**不再断言软重启无效**，只保留"断电最彻底"。
> 另：`led_position` 只反映「最后操作的灯索引」，
> **不代表全灭**，不能作为设备健康/灭灯判据（用 `led status` 的 effect 表或监控 `show_led` 事件）。

⚠️ **红外落地的真阻塞 = 网络（不是代码）**：
执行层 `deploy/lx06/ir/`（`ir_tx.py`/`ir_protocols.py`/`ir_probe.py`）已就绪且真机端到端验证通过，
但**只有 WSL 宿主能连到音箱**：

```
NAS(10.10.10.2, 有线) → LX06(10.10.10.20, AX6S 无线)   FAIL FAIL FAIL
WSL宿主(10.10.10.6)  → LX06                            9/9 OK
```

⇒ 在打通 NAS→LX06 **之前**，不要把红外接进 bridge（bridge 跑在 NAS docker 上）。
详见 [`../deploy/lx06/ir/README.md`](../deploy/lx06/ir/README.md) §1。

⛔ **禁止施工（结论已证伪，勿据此开发）**：

| 能力 | 为什么不行 |
|---|---|
| `mibrain aivs_event_post` 注入任意 AIVS 指令 | **恒返回 `code:1`**。需云端分配的 `dialog_id`，而 ubus policy 只有 3 字段 ⇒ 协议层无法传入 |
| 非 root 调 ubus | **root-only**。`/usr/share/acl.d` 不存在 ⇒ ubusd 对 `uid!=0` 一律 `PERMISSION_DENIED` |
| 用 `external_url` 替换云端 | 实测**文件不存在**，走编译期默认端点。**「自建后端」应是「扩展」而非「替换」**（见 §10 更正） |
| 依赖 `ai_crontab` 的 `duration` 做定时 | 实测**无效** |
| **按「LX06」名字推断 WiFi/BT 芯片** | 🔴 **本机（board_id=10）实载 8987，不是 8821cs**。`8821cs`/`sd8xxx`/`cfg80211`/`compat`/`bt8xxx`/`rtk_btuart`/`et6037` 共 **8 个模块永不加载**。判断依据用 `/proc/mwlan` vs `/proc/rtl8821cs` 的**存在性**，见 [`../docs/devices/lx06/kernel-surface.md`](../docs/devices/lx06/kernel-surface.md) §1 |

⛔ **禁止暴露（内核面，root-only 高危）**：

| 节点 | 为什么不能进 bridge 能力表 |
|---|---|
| `/proc/mwlan/adapter0/config` | 19 个内核命令字：`tx_continuous`+`channel`+`tx_power` = **定频干扰源**；`fwdump_file=<path>` = **任意路径 `O_TRUNC` 写**（root）；`sdcmd52rw` = 直发 SDIO CMD52 |
| `/proc/mwlan/adapter0/wlan0/debug` · `log` | 驱动调试写接口，会触发 dump / 刷屏 |

> ✅ 但 `/proc/mwlan/adapter0/wlan0/info` 是 `0444`（**非 root 可读**），含驱动级
> `num_tx/rx_bytes`、`num_*_dropped/err`、`channel`、`essid` ⇒ **推荐**作为健康探针接入；
> ⚠ 它同时泄露 MAC/BSSID/ESSID，**不得写日志或上报云端**。

**施工纪律**：
1. 接新方法前，先查逆向侧 `run/assumptions.md` 对应编号的 `INVALIDATED` 标记 —— **有 6 条结论标题仍是错的、正文已更正**（含「8821cs 是主 WiFi」）
2. `code:0` **不等于成功**（`player_play_url` 已实证），批量接入须加**状态回读确认**
3. 建议抽出一张「设备能力表」（方法名 + policy + 验证状态），避免 `speaker.py` 里硬编码膨胀

#### ⭐ 优先复用原厂 CLI：`mphelper`（别自己拼 ubus）

> 取证：逆向侧 `run/mphelper-playback-cli.md`；落地参考 [`../docs/devices/lx06/mphelper-playback-cli.md`](../docs/devices/lx06/mphelper-playback-cli.md)

原厂提供了一个**完整的播控 CLI**（`/usr/bin/mphelper`，shell 脚本），
把播放/音量操作都封装好了，且包含 bridge 自己拼 ubus 时**缺失的三样东西**：

| `mphelper` 提供 | bridge 自己拼 ubus 的缺口 |
|---|---|
| **voip 通话闸门**（`voip_call`） | 通话中也可能被 bridge 播放打断 |
| **声纹注册闸门**（`voice_print_registing`） | 声纹注册中可能被打断 |
| **按键提示音 + `aivs_track_post` 埋点** | 行为与原厂不一致（云端看不到这些操作） |

**常用命令**：`play` / `pause` / `toggle` / `toggle_quiet` / `next` / `prev` /
**`iot_next` / `iot_prev`**（无提示音无埋点，IoT 场景更干净）/ `mute_stat` /
`volume_get` / `volume_set N [beep]` / `continuous_volume_set` / `volume_up` / `volume_down` / `tone <url>`

**三条容易踩错的点**：
1. ⚠️ 取当前音量用 **`mediaplayer player_get_context`**（解析 `info.volume`），**不是** `get_media_volume`
2. ⚠️ **`mute_stat` 名不副实** —— 它返回 `player_get_play_status` 的 `info.status`（**播放状态**），不是静音状态
3. ⚠️ **LX06 的音量步进是 4**（`mphelper` 默认 4；仅 LX01/LX05A/LX05/L07A 是 8）；
   且原厂 `volume_up/down` **没做 0–100 clamp**，bridge 自己实现时要加
   ⚠️ **2026-10-20 修正**：「步进 4」**不严格** —— 实测档位 0/2/4/6 的 `get_media_volume` 全是 25，
   8/10 全是 40 ⇒ **存在平台段**，`v` 与 `v+4` 不一定差一档。
   ⚠️ 另：`player_set_volume` 的 `unit`/`type` 字段**不能切换量纲**（传 `unit:"percent"` 仍按档位映射）

⇒ **建议**：`speaker.py` 的播放/音量类操作**改调 `mphelper`**，
可免费获得上述闸门与一致性；同时修掉 §SpeakerManager 的 `set_mic` 语义反转。

### APIServer (core/services/api_server.py)

HTTP REST API 服务器（aiohttp），端口可配（默认 9092）。

| 端点 | 方法 | 功能 |
|------|------|------|
| `/api/play/text` | POST | 播放文本 |
| `/api/play/url` | POST | 播放 URL |
| `/api/play/file` | POST | 播放本地文件 |
| `/api/status` | GET | 获取设备状态 |
| `/api/wakeup` | POST | 唤醒设备 |
| `/api/interrupt` | POST | 中断播放 |
| `/api/health` | GET | 健康检查：默认返回录音帧龄/策略、播放事件面、注入状态；`?probe=1` 额外采集 mediaplayer 存活/内存/麦态/固件白名单字段（总超时 4s；会打设备 RPC，配置 `BRIDGE_API_TOKEN` 时须带 Bearer，未配置时 probe 503） |
| `/api/tts/doubao` | POST | Doubao TTS 合成 |
| `/api/music/sessions` | POST | 开托管会话（B-05；`MANAGED_TOKENS` 专用凭据 + target） |
| `/api/music/sessions/{id}` | GET | 会话快照（含 `last_seq` 游标） |
| `/api/music/sessions/{id}/commands` | POST | 托管命令（`idem_key` 幂等回执 + `expected_epoch`；202 首执 / 200 重放 / 409 冲突或过期代次） |
| `/api/music/sessions/{id}/events` | GET | SSE 事件流（`Last-Event-ID` 续传、缺口 `reset`、慢消费者 reset） |
| `/api/tts/doubao_voices` | GET | 获取音色列表 |
| `/api/audio_input` | POST | 「停止聆听」恢复通道：`{"mic": "on"/"off"}` 控制 KWS 分析开关（listen_disabled 持久，语音口令自身失效后唯一恢复路径） |

> 🔐 **业务鉴权（ticket 06 + 票 12 多 token，`core/services/auth.py` 纯函数 + `APIServer.api_auth_middleware`）**：
> 写类端点（上表除 `status`/`health`/`doubao_voices` 外的 POST，含 `/api/inject*`）要求
> `Authorization: Bearer <token>`（缺失 401 / 不匹配 403）。token 来源两级：
> `BRIDGE_API_TOKENS=label:token,...`（具名集合，命中 label 写进审计日志，可定点吊销）；
> `BRIDGE_API_TOKEN` 保留为单一 `legacy` 标签（迁移并存窗口，收口即删）。
> **未配置时写类端点 503 失败关闭**；**配置非法（格式错/label 重复/值重复）时整份 503
> `auth_config_invalid`**（半残配置不允许静默半放行）。`/api/health`、`/api/status`、
> `GET /api/inject`、`/api/inject/relay`、`/api/tts/doubao_voices` 仍可无鉴权读取。
> `ADMIN_TOKEN`（面板）与业务 token 分离、互不回退。新增端点默认按写类处理（失败关闭），
> 放行只读端点须显式加入 `auth.READ_ROUTES`。日志只记 label/调用方与错误码，不打印 token
> （写类放行一行 INFO `label= caller=`，拒绝一行 WARNING `code= label=none`）。

> 🧾 **审计调用方（ticket 09，`core/services/caller.py`）**：日志里的 `caller=` 默认取
> **TCP peer**（`request.remote`）。生产无反向代理，`X-Forwarded-For` / `X-Real-IP`
> 可被任意客户端伪造 ⇒ **只有显式 `TRUST_PROXY=1`** 时才读（取 XFF **最右一跳**，
> 即最近受信代理追加的值；最左值是客户端可伪造的）。
> 部署到受控反代之后再开这个变量，否则审计等于没有。

> 🔒 **架构级依据（2026-10-19 逆向互证）**：此路径的「唯一性」不只是"静音后喊不了"（功能推理），
> 而是**设备架构决定的** —— 两条独立证据：
> 1. **语音总线层**：LX06 的 protobuf（`libaivs-message-util.so`）**根本没有 `MIC_ON`/`MIC_OFF` 消息**
>    （`strings` 零命中；对比 L05B 同族固件**有**这两个）
> 2. **指令分发层**：`Microphone` 命名空间**只有 `TurnOff`，没有 `TurnOn`**
>    （`MicrophoneInstructionHandle::TurnOffHandle` + `UnknownHandle`）
>
> ⇒ 设备**不提供任何原生开麦通道**。`/api/audio_input` 因此是**架构级唯一路径，务必保持高可用，
> 不可视为可重构的临时方案**。取证：`docs/devices/lx06/ai-crontab-cmds.md` §4.1、
> 逆向侧 `run/l05b-cross-check-backref.md` §1。

### 后台管理面板 (core/services/admin_api.py)

内嵌 Web 控制台（`GET /admin`，单页零依赖），随 APIServer 同端口启动。

| 端点 | 方法 | 功能 |
|------|------|------|
| `/admin` | GET | 面板页面（总览 / 日志 / 设置三页） |
| `/api/admin/overview` | GET | 各系统状态总览（设备/后端/音频管线/外部服务） |
| `/api/admin/config` | GET/PUT | 读取（密钥掩码）/ 写入运行时覆盖层（白名单 schema，保存即热生效） |
| `/api/admin/config/test` | POST | 上游连通性预检（按所选规格：GET /models → 兜底各协议最小对话 ping） |
| `/api/admin/logs` | GET | 内存日志增量拉取（?after=<seq>） |
| `/api/admin/logs/level` | POST | 运行时调整日志级别 |

**安全与边界约束**:
- 所有 `/api/admin/*` 要求 `Authorization: Bearer <ADMIN_TOKEN>`；未配置该环境变量时一律 503 拒绝
- **不接受 `?token=`**（ticket 09）：查询串会进浏览器历史/反代 access log/Referer；
  面板前端走 localStorage + Authorization 头，其它客户端同样只走请求头
- 配置写入走白名单 schema（`CONFIG_SCHEMA`）+ **运行时覆盖层**，绝不文本改写 config.py 源文件
- 分层语义：面板覆盖值（`data/runtime-overrides.json`，原子落盘）> config.py/env 底层值；覆盖项值为 null 表示清除回落
- 密钥字段读取只回掩码（尾 4 位），明文不出服务端
- **接口规格**（`openai.api_style`）三选：`chat_completions`（默认）/ `openai_responses` / `anthropic_messages`；内部会话历史统一 chat 格式、出口按规格转换；连通性预检使用**本次提交的待测 Key** 构造鉴权头（不得复用运行时旧 Key）

### 指令层双表与执行器 (core/commands/ + config.py)

指令层内核化（C4/P2 ticket 01）后的分工：**逻辑面在 `core/commands/`（随镜像），数据面在 config.py（热重载）**。

| 表（config.py 数据） | 触发源 | 场景 |
|----|--------|------|
| `DIRECT_COMMANDS` | `before_wakeup(source=="kws")` 免唤醒短语 | 高频确定性播控/场景（毫秒级、100% 确定） |
| `XIAOAI_COMMANDS` | `before_wakeup(source=="xiaoai")` 小爱对话中截胡 | 「小爱同学，打开电脑」类口语触发自动化 |

- **config.py 只留纯数据**：两张指令表 + KWS 词表 + 人设表（`PERSONAS`）+ 路由钩子绑定（`before_wakeup` 一行委托 `core.commands.route_before_wakeup`），**不得出现执行逻辑或反向 import 核心单例**（`core.ref` 依赖已移除）；改表保存即热重载生效
- **core/commands/ 对外接口**：`route_before_wakeup(...)`（唤醒边界默认路由：表命中执行 / 停止聆听 / 人设切换 / 截胡清场）、`run_steps(speaker, steps)`（步骤序列执行器）、`ha_script`/`ha_switch`（声明式动作构造器）、`hass_action`（HA REST client，失败只记日志不打断语音流程）、`audit_kws_coverage()`（词表 ⊇ 指令表键对账，P2 票 02）
- **词表对账（P2 票 02，`core/commands/audit.py`）**：开机（`MainApp.run`）+ 每次配置热重载（reload listener）各跑一次；缺失输出含全部键名的单条 WARN，**只告警不阻断**。对账口径 = kws 路由面：`DIRECT_COMMANDS` 键 + `STOP_LISTENING_KEYWORD` 必须在 `wakeup.keywords`（KWS 认不出就永远到不了路由表）；`XIAOAI_COMMANDS` 走小爱原生 ASR 截胡、不经过 KWS，**不在对账范围**；`PERSONAS` 子串匹配不要求整词入表。数据源不可用（config 模块缺表等）降级跳过不崩溃。守门测试：`tests/test_kws_audit.py`（含 deploy 真实数据零缺失守门——改表忘同步词表即红）
- **动作元素三型**（混排，按序执行）：`str`（经 `announce` 双闸门 + 词表默认拒绝；首条充当确认播报）→ `("ha", domain, service, data)` 元组（构造器产出）→ 任意 `async def(speaker)` 函数
- **契约测试**：`tests/test_commands.py` 纯内存覆盖（三型混排次序 / 失败传播 / 清场→播报→执行→收尾 / 路由语义 / deploy 数据面守门）
- **截胡边界**：米家设备控制类口语（「把空调开到 26 度」）放行原生小爱，两表都不截——判定表见工作区 `selection-review.md` §3.1.2；边界错了就是吞指令
- **执行前清场**：`speaker.stop_device_audio()`（`core/commands/router.py clear_device_audio`）；**禁止** `abort_xiaoai()`（见「不可用的中断方式」）

### Agent 工具层 (core/tools/)

LLM function calling 的内嵌工具注册表（lineage 阶段 1.5，需求⑤"任务执行"的实现层）。

| 文件 | 职责 |
|------|------|
| `registry.py` | 三函数：`get_all_tools()`（聚合工具定义喂 LLM，D 级对模型隐藏）/ `execute_tool(name, arguments)`（责任链分发，D 级拒绝留证）；**未声明合法 RISK 的模块拒绝注册**（P3 票 03 起为注册闸门） |
| `guardrails.py` | **写操作护栏框架（P3 票 03）**：RISK 分级 read/a/b/c/d、`execute_guarded` 全路径（A 复述即执行 / B 范围校验+显式应答 / C 二次确认三分支 / D 拒绝）、`ConfirmationManager`（TTL/顶替/惰性过期）、`sanitize_for_speech`（复述文案白名单注入防线）、`match_confirmation`（否认词优先） |
| `hass.py` | HA 实体状态查询（只读，RISK=read） |
| `weather.py` | Open-Meteo 天气查询（无需 key，已端到端验证，RISK=read） |
| `music.py` | LMS 播放状态查询（只读，RISK=read） |
| `song_request.py` | 在线点歌 + 整张专辑（**RISK=a**，B-33/B-35）：kwqq-api `search`/`album` → `song/url`（320k）→ 设备播放；内存队列 next/prev/stop，桥队列为空时回落设备自身队列；`enabled` dev 默认关 / deploy 开 |
| `device_status.py` | 音箱自身状态聚合（播放/音量/麦/内存/夜间模式，只读，P3 票 01；miot 周边发现不可达 MIO-6，家庭其他设备走 hass） |
| `ir_send.py` | 红外发射（**RISK=c**，P3 票 04）：LLM 只传 device/action，码值查 `ir.codes` 配置表（外部提供）；幻觉 pattern 被忽略；speaking 态拒绝 + 串行锁（关中断纪律） |

**接线位置与硬约束** (`core/openai.py`):
- `tools` 注入仅 **chat_completions 风格**；回环深度 `_max_tool_rounds=3`（配置项 `max_tool_rounds`），总开关 `tool_calls_enabled`（默认开）
- ⚠ **reasoning_content 条件回传**：thinking 模式带 `tools` 时，后续每轮必须完整回传 assistant 的 `reasoning_content`（缺失直接 400）；仅当响应携带时才回传（非 thinking 模型无此字段，天然无影响）——改消息组装前先读官方 thinking_mode 文档
- **写操作分级纪律（P3 票 03 起）**：新增工具必须声明 `RISK`（read/a/b/c/d），否则拒绝注册；B/C 级必须经 `guardrails.execute_guarded` 的显式应答流程；D 级永不开放（模型不可见 + 调用拒绝）；复述文案过 `sanitize_for_speech` 后才能进 TTS/announce
- **流式响应通道（P1 ticket 03）**：`stream_response_segments`（基类默认 = 非流式「单句序列」退化；OpenAI 子类在 `openai.stream` 配置开 + chat_completions 风格下走 SSE）；**工具轮探测降级**——带 tools 的流式请求首见 tool_calls 增量且未产出句段即转非流式回环（回环路径与改前逐字一致），模型不调工具的终答轮流式到底；已产出句段后到达的 tool_calls 增量丢弃（按纯文本终答，WARNING）；上游异常吞错降级不杀会话（与非流式同口径，仅完整回复入历史）；开关默认关，面板 `openai.stream` 热切换即回退

### 音频处理链

| 模块 | 文件 | 职责 |
|------|------|------|
| GlobalStream | `audio/stream.py` | 多路输入广播（模拟 PyAudio API） |
| VAD | `audio/vad/`（`__init__.py` + `silero.py`） | Silero ONNX 语音活动检测 + **公开订阅 seam**（P2 票 04：`subscribe_events`/`subscribe_frames` 多消费者、注销幂等、订阅者异常隔离；回调在检测线程同步上抛；第二消费者一行挂上） |
| KWS | `audio/kws/sherpa.py` | Sherpa ONNX 关键词唤醒（信心度 2.0，阈值 0.2）；`kws/__init__` 含 `listen_disabled` 持久开关 + **播放闸门**（`gate_on`/`gate_off`） |
| 多声道入口 | `audio/channels.py` | 6ch 帧对齐 + 最强麦（滑窗+迟滞）/等权平均/固定声道降为 1ch；纯 stdlib |
| 录音看门狗 | `audio/watchdog.py` + `core/xiaoai.py` | 1s 无帧判死 → 先停后启 + 指数退避；`audio_health()` 暴露帧龄/策略/恢复计数 |
| ASR | `audio/asr/sherpa.py` | Sherpa SenseVoice 离线语音识别（懒加载，INT8 量化） |
| TTS | `tts/openai.py`, `tts/mlx_audio.py`, `tts/doubao.py` | OpenAI-compatible TTS、MLX-Audio 与豆包 TTS（一次性/本地音频播放） |

### 音频 / 事件面 / 注入配置

- `audio_input.channels`（默认 6）、`channel_strategy`（strongest/average/fixed）、
  `fixed_channel`、`watchdog_seconds`（默认 1.0）；bridge 自动把声道数同步到 native
  的 `AUDIO_INPUT_CHANNELS`，改 1 即回退单声道。
- `event_plane.source`（context/client/native，默认 context）、`shadow_sources`
  （默认 ["client"]）、`poll_interval_ms`、`start_grace_ms`、`native_cmd`（默认空 =
  不启动 native 源；运行时镜像无 ssh，勿加依赖）。适配器支持热重载。
- **托管播放（B-04/B-05，设备执行未接线）**：`core/music/` 提供 `TrackRef`/`QueueEntry`/
  `PositionSnapshot`/`SessionState` + `ManagedStore`（SQLite，`data/managed/state.db`，
  一次事务写全会话；`restore()` 只还原元数据并把会话置 `recovery_pending`，绝不授予
  恢复资格）+ `ManagedExecutor`（有界单 worker；`stop` 只改代次不排队；迟到结果 →
  `UNKNOWN` + 一次补偿 Stop，不重发）。位置快照的 `transport`（dlna/rpc）与
  `player_backend` 是两个维度，写入时校验一致。B-05 已提供 `ManagedService`（幂等回执、
  代次闸门、状态+事件+回执同事务）与 `/api/music/*`（SSE、`MANAGED_TOKENS` 凭据、端点+target
  scope、失败关闭；每次命中最外层持有一条端到端预算 `MANAGED_COMMAND_BUDGET_MS`
  （子层只能消耗剩余、不得叠加，耗尽 504 `budget_exhausted`）；设备确认后
  `observe()` 把回执从 accepted 推进到 observed 并写事件；CORS 默认关闭，
  `MANAGED_CORS_ORIGINS` 白名单才放行预检）。B-06 起 `core/music/player.py` 提供 EOF 判定与队列推进：
  **不依赖自然 STOPPED**——按「可信时长 + 新鲜位置 + 当前意图」到点后主动 Stop 并回读确认，
  再换当前 entry；单条只推进一次，末条停止；暂停冻结计时、位置陈旧 ⇒ stalled、时长未知 ⇒
  unknown；回绕补偿仅限声明支持的 backend 且只一次；外部接管 ⇒ relinquish 不抢回。
  `backends.py` 的 DLNA（主）与 RPC（降级）各自声明 position/seek/resume/stop 能力，
  未验证的一律 `Unsupported`。`runtime.py` 的 `ManagedRuntime` 按间隔驱动 tick（推进后
  自动起播下一条）。**迁移开关**：`song_request.managed`（config，默认 false）——
  true 时 `song_request.run()` 走 `core/music/song_bridge.py`（搜索/专辑候选逻辑照旧，
  队列归托管 store、单所有权、无第二自动推进）；false = 旧内存队列（回滚模式）。
  **仍未完成**：真机 loop 0/1、位置/时长与 Stop 回读验收（B-08）。
- **TTS focus（B-07，离线核心）**：`tts_focus.py` 的 `TtsFocus.acquire()` **先暂停并回读确认**
  才发令牌；恢复资格绑定 session/epoch/entry/focus token——用户 pause/stop/next、外部接管、
  代次变化、二次播报换令牌都会让旧令牌失效，`release()` 只给原因（`stopped`/`epoch_changed`/
  `focus_superseded`/`entry_changed`/`external_takeover`/`resume_unsupported`），绝不偷偷 Play。
  `speak()` 对每个 provider 独立给结果（ok/failed/cancelled/unknown），unknown 或 weak 先做一次
  **安全停止**再交回调用方；`tts_providers.py` 给 doubao/openai/mlx_audio（各自 Rust 播放令牌）
  与 xiaoai/脚本回退（announce + 停设备音频）的适配。**已接线**（`focus_bridge.py`）：
  `speaker.announce` 在词表检查通过后取 focus（`required`：音乐在播但暂停确认不了 ⇒
  `AnnounceRejected`，绝不两路抢设备），finally 先释放 focus 再归还 KWS 闸门（恢复音乐不会
  落在闸门关闭窗口）；对话回复段（`_speak_response_segments`）取 focus 但**非 required**
  （focus 失败不阻断回复），异常路径同样释放。麦态/KWS/录制的 finally 归还原语义不变
  （`mic_gate` 按进入时快照恢复，用户停止聆听保持 off）。
- **完成证据（B-03）**：`wait_for_finish()` 返回 `Completion(finished, started, reason,
  quality, source, paused)`（属性访问，不再支持三元组解包）。`quality=strong` ⇔ 主源
  亲自观察到起播**和**结束；影子兜底 / 宽限兜底 / 超时 / 未见起播一律 `weak`。
  `_wait_track_end` 只认 `is_strong` 且未暂停的完成，**超时不靠读回补成完成**；
  `context` 的 status=2 按**暂停**（不是空闲）处理，暂停后未恢复的 idle 不算播完。
  跨曲排除旧事件：在**下发播放命令之前**取 `begin_playback()` 并把序号传给
  `wait_for_finish(since_seq=...)`（选曲播报在 URL 起播前取号，播报自身事件不计入）；
  旧 `paused` 不再给「已在播放」背书。native：`prepare()` 做有界就绪确认（进程存活），
  零行退出按 5s→10s→…（上限 60s）退避，每个退避窗口最多强制重启一次。
- `inject.enabled`（默认 false）或环境变量 `INJECT_ENABLE`：关闭时不监听 `:9093`，
  注入端点 503；开启时窗口 = 闸门 + 中继 + 唤醒，三者同开同关。
  ⚠️ **开启 ≠ 可裸奔（票 12）**：未配 `INJECT_ALLOW_CIDR` 且未显式 `INJECT_ALLOW_ANON=1`
  ⇒ **拒绝监听 :9093**（`inject data plane: auth=refused`，共享密钥首包待 hook 改动）。
  CIDR 命中才接管推流（先认证后推流），拒绝/替换均有 WARNING；当前模式看
  `GET /api/inject` 或 `/api/health` 的 `relay.auth`（cidr/anon/refused）。

### Rust 原生扩展 (native/)

通过 maturin + PyO3 编译的 `open_xiaoai_server` Python 模块。

| 文件 | 职责 |
|------|------|
| `lib.rs` | 模块入口：`on_output_data`, `start_server`, `stop/start_recording`, `stop/start_playing`, `run_shell` |
| `server.rs` | TCP :4399 WebSocket 服务器，处理音频流和事件路由 |
| `python.rs` | Python 回调注册中心（`register_fn` / `call_fn`），跨语言调用 |
| `tts/` | TTS 音频处理：HTTP 流式请求、MP3 解码、PCM 直通 |

## 运行模式

### 模式 1: 仅小爱（默认）
```bash
uv run main.py
```
- 不启动 KWS/VAD 初始化
- `core/services/audio/kws/keywords.py` 在此模式下应直接退出成功

### 模式 2: 小智 AI
```bash
XIAOZHI_ENABLE=1 uv run main.py
```
- 启动 VAD + KWS，唤醒后连接小智 AI
- KWS 初始化失败应视为启动失败

### 模式 3: OpenClaw
```bash
OPENCLAW_ENABLE=1 uv run main.py
```
- 小爱指令拦截 → 转发到 OpenClaw → TTS 播放结果

### 模式 4: 小智 + OpenClaw（混合）
```bash
XIAOZHI_ENABLE=1 OPENCLAW_ENABLE=1 uv run main.py
```
- config.py `before_wakeup` 按唤醒词路由到小智或 OpenClaw 连续对话
- OpenClaw 连续对话：VAD → ASR → OpenClaw → TTS 循环
- 退出关键词：config `openclaw.exit_keywords`

### 启用 API Server
```bash
API_SERVER_ENABLE=1 uv run main.py
```

## 开发规范

### 代码风格
- 中文注释和文档字符串
- 英文 commit message
- 类型提示: `dict[str, asyncio.Future]`

### 异步编程
- 所有 I/O 使用 `async/await`
- 线程安全使用 `asyncio.run_coroutine_threadsafe()`
- `MainApp.loop` 是业务协程主循环
- `XiaoAI.async_loop` 仅用于原生扩展回调桥接，不挂新业务状态机

### 日志规范
- 所有日志必须带模块标识：通过 `module=` 参数或 `[Module]` 前缀
- 使用 `core.utils.logger.logger`，禁止裸 `print`
- 调试输出用 `DEBUG` 级别，不污染 `INFO`
- 消息体不要重复模块名（模块名已在日志前缀中）
- 唯一允许的裸输出：启动 ASCII banner

### 全局引用 (ref.py)
- `set_app/get_app`, `set_xiaozhi/get_xiaozhi`, `set_xiaoai/get_xiaoai`
- `set_vad/get_vad`, `set_kws/get_kws`, `set_speaker/get_speaker`
- `set_audio_codec/get_audio_codec`, `set_speech_frames/get_speech_frames`

### 兼容约束
- `CLI` 环境变量不再作为功能开关，不要引入依赖 `CLI` 的运行时分支
- `XIAOZHI_ENABLE=0` 时必须允许跳过 KWS 初始化
- `scripts/start.sh` 在仅小爱模式下不应检查 `core/models/` 下的模型文件
- 工具回环三开关缺一即回退纯对话：`tool_calls_enabled` / `get_all_tools()` 非空 / api_style 为 `chat_completions`——排障时按此顺序查
- `reasoning_content` 守卫只能收紧不能放松：thinking+tools 场景漏回传 = 400（详见「Agent 工具层」）

## 构建与部署

- **镜像必须本地构建**：fork 深度演进后，`kws/__init__.py`、`api_server.py`、`openai.py`、`tools/` 等代码改动**烤进镜像**，bind-mount（config.py/models）覆盖不到——拉上游镜像 = 丢功能。构建与验收流程见 [deploy/REBUILD.md](deploy/REBUILD.md)
- **家庭部署手册**（NAS 部署 / LX06 接入 / 验收 / 安全）：[deploy/README.md](deploy/README.md)
- **HA 配置片段权威**：工作区 `deploy/ha/`（按代际编号）——本仓库内不含 HA 配置
- `config.py` 有两份：根目录 = 开发基线；`deploy/config.py` = 生产版（scp 到群晖，热重载生效）——改错文件是已踩过的坑（首次部署只推镜像漏推 config.py → 唤醒失败）

## 测试

```bash
# 无音箱流式冒烟测试
python3 tests/test_tts_stream.py

# 比较长文本 mp3/pcm 流式时延
python3 tests/test_tts_latency.py --formats mp3,pcm --rounds 3 --repeat 8

# OpenClaw 连通性测试
python3 tests/test_openclaw_live_connectivity.py

# 后台面板冒烟测试（无需音箱/模型/Rust 扩展；用法见脚本头注释）
python3 tests/test_admin_panel.py
```

## 音箱设备控制命令

小爱音箱（LX06 等）基于 OpenWrt + busybox，设备端命令和行为如下：

### 音频播放通道

音箱上有多条独立的音频播放通道，中断时需要分别处理：

| 通道 | 进程/服务 | 触发方式 | 中断方式 |
|------|-----------|---------|---------|
| PCM 直通 | `aplay` | `open_xiaoai_server.start_playing()` → WebSocket stream | `open_xiaoai_server.stop_playing()` |
| 阻塞 TTS | `tts_play.sh` → `miplayer -f <file>` | `speaker.play(blocking=True)` | `killall tts_play.sh miplayer` |
| 非阻塞 TTS | `mibrain_service` (内部播放) | `speaker.play(blocking=False)` → `ubus call mibrain text_to_speech` | `mphelper pause`（不一定可靠） |
| 媒体播放器 | `mediaplayer` (系统守护进程) | `ubus call mediaplayer player_play_url` | `mphelper pause` / `ubus call mediaplayer player_play_operation '{"action":"pause"}'` |

### tts_play.sh 工作流程

> **默认阻塞播报已不走此脚本**（ticket 03）：优先 `text_to_speech {save:1,play:0}` 落盘 →
> `player_play_url file://<path>` → 回读 `player_get_context.info.status` 到空闲 →
> `rm -f /tmp/tts/<path>` 清理。`tts_play.sh` 仅在合成失败时兜底；
> 兜底路径的成败判据已登记进能力表（P0 ticket 01）：rc∈{0,1}（成功时 rc 也可能是 1）+ stderr 出现 `Failed to parse` 判失败，业务层只读结构化 `ok`。

`/usr/sbin/tts_play.sh` 是设备上的阻塞 TTS 脚本，内部流程：
1. `mphelper pause` — 暂停当前播放
2. `ubus call mibrain text_to_speech '{"text":"...","save":1}'` — 生成音频文件到 `/tmp/tts/`
3. `miplayer -f <path>` — 播放音频文件（子进程）
4. `rm <path>` — 清理临时文件

**关键注意事项**：
- 杀掉 `tts_play.sh` **不会**自动杀掉子进程 `miplayer`，必须同时 `killall miplayer`
- `miplayer` 是一次性播放器（非守护进程），杀掉后不影响后续 TTS 调用
- busybox 的 `pkill` 无法匹配到 `miplayer`，必须用 `killall`

### 录音通道

| 操作 | 命令 | 说明 |
|------|------|------|
| 停止录音 | `XiaoAI.stop_recording()`（包装 native） | 杀掉设备端 `arecord`；同时标记看门狗 expected=false |
| 恢复录音 | `XiaoAI.start_recording()`（包装 native） | 重启 `arecord`；标记 expected=true，音频恢复流入 `GlobalStream` |

**注意**：会话层通过 `XiaoAI` 包装调用，不要直接调 `open_xiaoai_server`，否则看门狗的 expected 标志不会更新。
client 的录音读循环 **500ms 无数据即退出且不通知服务端**；`core/xiaoai.py` 看门狗 1s 无帧执行
「先停后启」，失败按指数退避重试，状态见 `XiaoAI.audio_health()`。

OpenClaw 对话中 TTS 播放时会 `stop_recording` 防止回声。如果在此期间触发中断（"小爱同学"），必须在中断处理中调用 `start_recording` 恢复录音，否则 KWS 将因无音频数据而永久失效。

### on_interrupt 中断处理要点

`on_interrupt()` 触发时（用户喊"小爱同学"），需要完成以下步骤：
1. Cancel OpenClaw asyncio task
2. 让 OpenClaw controller 自己停止当前 TTS（使用自己持有的 `playback_token`）
3. `SpeakerManager.stop_device_audio()` — 停止阻塞 TTS / 非阻塞 TTS / PCM，并重置 PCM 通道
4. `start_recording` — 恢复录音（KWS 依赖此通道）
5. `XiaoAI.stop_conversation()` — 停止连续对话

### KWS 播放闸门（TTS 自触发回环防护）

**问题**（2026-09-17 事故，见工作区 `docs/plan/incident-kws-self-trigger-loop.md`）：
TTS 播报期间 KWS 从未被暂停 → 播报文案命中自身词表 → 音箱听见自己 → 自触发回环。

**实现**：`core/services/audio/kws/__init__.py`

| 方法 | 作用 |
|---|---|
| `gate_on(reason, max_seconds)` | 开闸门（引用计数 +1），返回 token；挂兜底 timer |
| `gate_off(token)` | 关闸门（-1）；归零时 reset Sherpa 流 |
| `paused`（派生属性） | `_conv_paused or 闸门计数>0` —— 会话暂停与播放闸门**正交** |
| `gate_status()` | `(计数, 原因列表)`，供可观测性 |

**落点**（P0 ticket 02/03 起全部收敛到 `speaker.announce`）：
1. `speaker.announce(...)`（`core/services/speaker.py`）—— **唯一权威位置**：双闸门 + 词表安全检查 +
   完成等待 + finally 归还；`/api/play/text`（票 02）、`/api/play/url`、`/api/play/file`、
   `deploy/config.py` 的 `_run_steps`（均票 03）已全部切换
2. ~~各入口的 mic_gate/`gate_on` 手工编排~~ 已删除——闸门知识只活在 announce 与 KWS 类内

**硬约束**：
- 必须 `try/finally`（异常也要 `gate_off`），否则 KWS 永久失效
- 闸门归零与 `resume()` 恢复时都必须 `SherpaOnnx.reset()`，丢弃播报回声帧
  （AGENTS.md「VAD 状态泄漏陷阱」同一教训）
- 兜底 timer 防调用方遗忘/异常导致 KWS 永久失效
- **播报文案不得包含任何 KWS 词表词**（见下）

### TTS 文案安全约束（自触发防护第二道）

**播报文案绝不可包含 KWS 词表（`config.py` 的 `wakeup.keywords` / `DIRECT_COMMANDS` 键）中的任意子串。**
历史教训：`"正在开启高保真模式"` 命中 `高保真模式`+`开启高保真`；`"高保真模式已开启"` 命中 `高保真模式`。
现文案已改为 `"正在准备客厅音响"`（bridge）/ `"客厅音响已就绪"`（HA `nuc_tts`）。

新增/修改任何 TTS 文案后，必须跑一次词表交叉检查（`tests/test_kws_gate.py` 之外的手工清单见
工作区 `docs/plan/incident-kws-self-trigger-loop.md` §5 建议 2）。
> ✅ **P0 ticket 02（2026-10-25）**：该检查已自动化——`speaker.announce` 对每次播报文案现取
> `wakeup.keywords` + config 模块指令键两组词源做子串交叉，命中默认拒绝（`AnnounceRejected`）；
> 手工清单降级为兜底（覆盖不经 announce 的路径）。

### 不可用的中断方式

以下方式在实践中验证**不可靠或有副作用**：
- `abort_xiaoai()`（重启 `mico_aivs_lab`）— 会导致小爱整体不可用，恢复需 1-2 秒
- `pkill miplayer` — busybox `pkill` 无法匹配 `miplayer` 进程名
- `ubus call mediaplayer player_play_operation '{"action":"pause"}'` — 对 `mibrain text_to_speech` 触发的播放无效

### 相关讨论

- [open-xiaoai#36](https://github.com/idootop/open-xiaoai/issues/36) — 小爱 TTS 打断方案讨论

## 参考资源

- 项目主页: https://github.com/coderzc/open-xiaoai-bridge
- 刷机教程: https://github.com/idootop/open-xiaoai/blob/main/docs/flash.md
- Client 端补丁: [docs/client-rust.md](docs/client-rust.md)（协议契约、idootop/coderzc 版本谱系、部署与更新判据、已知缺陷——排障/升级 client 前先读）

## Agent skills

### Issue tracker

Issues are local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: `GLOSSARY.md` at repo root + `docs/adr/`. See `docs/agents/domain.md`.

## 测试运行口径（2026-10-25 成文）

裸 `python3 -m unittest` 会因缺依赖报 ImportError（aiohttp/websockets 等）。完整口径（WSL 侧）：

```bash
cd <bridge根目录>
uv venv .venv-test && uv pip install --python .venv-test/bin/python aiohttp websockets requests cryptography numpy
PYTHONPATH=. .venv-test/bin/python -m unittest discover -s tests -t tests -p "test_*.py"
```

当前基线：**934 tests OK (skipped=3)**（2026-10-29）。口径：b6ae705 上为 810 项（含 5 个夹具滞后失败，B-01 补齐两处对话 fake 的 `note_raw_turn_text` 后全绿），B-02 新增 18 项设备命令 quoting / loop 闸门 = 828，B-03 新增 18 项完成代次/质量门用例 = 846，B-04 新增 11 项托管 store/executor = 857，B-05 新增 19 项命令/事件/SSE = 876，B-06 新增 27 项 EOF/后端矩阵/运行期/迁移 = 903，B-05 收尾（预算/observed/CORS）新增 6 项 = 909，B-07 focus/四 provider 新增 18 项 = 927，接线新增 7 项 = 934。历史：706（2026-10-27，B-31/B-32 批后）。每次 main 合入后应复跑此基线；现场树若含他票未提交的测试工作，计数会高于基线，按文件归属折算。onnxruntime/sherpa-onnx 等重依赖仅功能运行需要，测试不需要（`test_pipeline.py` 自带 sherpa_onnx stub）。
