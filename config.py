# 本地开发基线配置；生产部署版见 deploy/config.py（家庭定制，scp 到群晖 /volume2/docker/open-xiaoai-bridge/）
#
# 范式纪律（C4/P2 ticket 01 起生效）：
#   config 只留「表数据 + 路由钩子绑定」，执行逻辑在 core/commands/；
#   生产范式的权威样例 = deploy/config.py（DIRECT_COMMANDS / XIAOAI_COMMANDS /
#   PERSONAS 三张表 + 一行委托的 before_wakeup）。
#   播报一律经 speaker.announce（双闸门 + 词表安全检查，命中默认拒绝并留可见日志），
#   不要裸调 speaker.play——文案命中 KWS 词表会冒自触发回环风险
#   （见上层仓库 docs/plan/incident-kws-self-trigger-loop.md）。
#   禁用 speaker.abort_xiaoai()（重启 mico_aivs_lab，小爱整体不可用 1-2s，
#   见 AGENTS.md「不可用的中断方式」）；截胡前的原生重叠应答用
#   clear_device_audio（只停播放、可逆）。

import asyncio
import socket
import subprocess
import sys
import time
import uuid

import requests

from core.commands import clear_device_audio, route_before_wakeup


# 每次唤醒生成独立 Session，对话互不干扰
# 适合以下场景：
#   - "提问 → 回答"式交互，不需要 Agent 记住上下文
#   - 长期使用同一 Session 导致 Agent 上下文窗口堆积过长，影响响应质量和速度
# def new_session_key():
#     return f"agent:main:session-{uuid.uuid4().hex[:8]}"


# ---- 唤醒词路由表（数据）：命中子串 → 进入对应后端连续对话 ----
# KWS 命中唤醒词后，开场白经 announce 播出（词表检查默认拒绝）并路由到对应后端。
# 需要专属会话/人设时给条目加 "session_key"（和可选 "system_prompt"），格式照抄
# deploy/config.py 的 PERSONAS 表；无 session_key 的条目 = 纯路由。
WAKEWORD_ROUTES = {
    "龙虾": {"backend": "openclaw", "intro": "龙虾来了"},
    "小黑": {"backend": "openai", "intro": "小黑来了"},
    "小爪": {"backend": "qwenpaw", "intro": "小爪来了"},
    "小智": {"backend": "xiaozhi", "intro": "小智来了"},
    # 音乐生态点歌触发（V2，纯中文 >=4 字）：免唤醒进 AI 对话报歌名，
    # 由 song_request 工具走 kwqq-api 播放；不放宽 kws 阈值（台账 #4 待裁决）。
    # 词表无热重载（KeywordSpotter 启动构建），识别生效待重启窗口。
    "我要点歌": {"backend": "openai", "intro": "想听什么歌"},
    "帮我点歌": {"backend": "openai", "intro": "想听什么歌"},
}


# ---------- 在线点歌队列的原生唤醒控制 ----------
# 点歌播放期间用户说「小爱同学，下一首 / 上一首」：原生小爱没有 bridge 的
# 播放队列，这里直接驱动 song_request（有队列才接管，无队列放行原生）。
_SONG_PREV_WORDS = ("上一首", "上一曲", "换回")
_SONG_NEXT_WORDS = ("下一首", "下一曲", "换一首", "换首歌", "换一个")


async def _speak_queue_failure(speaker, message: str) -> None:
    """切歌失败时把原因播出来（此时没有音乐在放，播报不会打断任何东西）。"""
    try:
        async with speaker.announce(
            text=message,
            reason="song queue failure",
            wait=False,
            gate_seconds=30,
        ):
            await speaker.play(text=message)
    except Exception:  # noqa: BLE001 - 词表拒绝/设备异常都不许冒泡
        try:
            await speaker.play(text=message)
        except Exception:
            pass


async def _song_queue_from_wakeup(speaker, text: str) -> bool:
    """原生唤醒文本命中队列控制词时执行切歌；返回是否已接管。"""
    try:
        from core.tools import registry, song_request

        if not song_request.has_queue():
            return False
        utterance = text or ""
        if any(word in utterance for word in _SONG_PREV_WORDS):
            action = "prev"
        elif any(word in utterance for word in _SONG_NEXT_WORDS):
            action = "next"
        else:
            return False
        registry.reset_reply_suppression()
        result = await song_request.run({"action": action})
        from core.utils.logger import logger

        logger.info(
            f"[config] song queue action from native wake: {utterance!r} -> {result!r}"
        )
        if not registry.reply_suppressed() and result:
            await _speak_queue_failure(speaker, result)
        return True
    except Exception as exc:  # noqa: BLE001 - 路由钩子绝不杀唤醒链路
        from core.utils.logger import logger

        logger.warning(f"[config] song queue wakeup route failed: {exc}")
        return False


async def before_wakeup(speaker, text, source, app):
    """
    处理收到的用户消息，并决定是否唤醒 AI。

    参数：
        speaker : SpeakerManager，可调用 announce/play/wake_up 等方法
        text    : 识别到的文字内容
        source  : 唤醒来源
                    'kws'    — 本地关键词唤醒（用户说了唤醒词）
                    'xiaoai' — 小爱同学收到用户语音指令
        app     : MainApp 实例，可调用 send_to_* / set_*_session_key 等方法

    返回值：
        "openclaw" — 进入 OpenClaw 连续对话流程
        "openai"   — 进入 OpenAI 兼容服务连续对话流程（例如 Hermes Agent API Server）
        "qwenpaw"  — 进入 QwenPaw 连续对话流程
        "xiaozhi"  — 进入小智 AI 流程
        None       — 不做额外处理（可在此自行调用 app.send_to_* 等）

    ---
    动态切换 session_key：
        每次进入此函数前，框架会自动将 session_key 重置为配置文件中的默认值。
        如需路由到其他 Agent，在 return 之前调用：
            app.set_session_key("openclaw", "agent:main:xxx")
        不调用则自动使用各后端 session_key 的默认值，无需手动重置。
    """
    if source == "xiaoai" and await _song_queue_from_wakeup(speaker, text):
        return None

    if source == "kws":
        # 唤醒词路由走 core.commands 默认分发（人设/开场白/停止聆听都在那里）
        return await route_before_wakeup(speaker, text, source, app, personas=WAKEWORD_ROUTES)

    if source == "xiaoai":
        # 「召唤X」→ 进入对应后端连续对话（原生收尾由框架 reset_all_sessions 处理，
        # 不需要也不应该 abort_xiaoai）
        if text == "召唤龙虾":
            return "openclaw"
        if text == "召唤小黑":
            return "openai"
        if text == "召唤小爪":
            return "qwenpaw"
        if text == "召唤小智":
            return "xiaozhi"

        # 一次性指令：前缀命中即清场（压掉原生重叠应答）后转发。
        # app.send_to(后端, 文本, and_play=…) 是统一入口：and_play=True 播报回复
        # （追加 rule_prompt），False = 纯发送（Agent 自主播报，追加 rule_prompt_for_skill）。
        if "让龙虾" in text:
            # One-shot: send to OpenClaw and play the reply via TTS
            await clear_device_audio(speaker)
            await app.send_to("openclaw", text.replace("让龙虾", ""), and_play=True)
            return None  # No further handling by the framework

        if "告诉龙虾" in text:
            # Fire-and-forget: let the Agent decide when/how to reply
            await clear_device_audio(speaker)
            await app.send_to("openclaw", text.replace("告诉龙虾", ""))
            return None

        if "让小黑" in text:
            await clear_device_audio(speaker)
            await app.send_to("openai", text.replace("让小黑", ""), and_play=True)
            return None

        if "让小爪" in text:
            await clear_device_audio(speaker)
            await app.send_to("qwenpaw", text.replace("让小爪", ""), and_play=True)
            return None

    return None


async def after_wakeup(speaker, source=None, session_key=None):
    """
    退出唤醒状态

    - source: 退出来源
        - 'xiaozhi': 小智对话超时退出
        - 'openclaw': OpenClaw 连续对话退出
        - 'openai': OpenAI 兼容服务连续对话退出
        - 'qwenpaw': QwenPaw 连续对话退出
    - session_key: 当前 OpenClaw/OpenAI/QwenPaw 后端 session_key
        可据此区分是哪个 Agent 退出，例如播放不同的退出提示语
    """
    # 退出提示语同样经 announce（词表检查）；不想要提示语就整段留空
    if source == "openclaw":
        # 示例：按 agentId 区分退出提示语
        # 所有后端的 session_key 已统一为 agent:<agentId>:<rest> 格式，第二段即 agentId。
        # 仍建议做越界防护，以兼容自定义的非标准 session_key。
        # parts = session_key.split(":") if session_key else []
        # agent_id = parts[1] if len(parts) > 1 else None
        # if agent_id == "assistant":
        #     text = "助手，再见"
        # elif agent_id == "xiaomei":
        #     text = "小美，再见"
        # else:
        #     text = "再见"
        text = "龙虾，再见"
    elif source == "openai":
        text = "小黑，再见"
    elif source == "qwenpaw":
        text = "小爪，再见"
    elif source == "xiaozhi":
        text = "小智，再见"
    else:
        return
    async with speaker.announce(text=text, reason="after_wakeup bye"):
        await speaker.play(text=text)

APP_CONFIG = {
    "conversation": {
        # 对话内旁路文本播报（「抱歉，我没有收到回复」/ C 级确认应答 / TTS
        # 失败 fallback）是否经 announce 双闸门（mic_gate + KWS 闸门）。
        # 默认关 = 现状裸 play；真机 A/B（轮间隙比对）后再转正（B-29 步 3）。
        "bypass_play_via_announce": False,
    },
    "song_request": {
        # 语音点歌（kw-qq-music-api，票 01）：搜歌 + 320k 直链经 LX06 播放。
        # enabled 默认关：真机验收窗口由用户打开（面板运行时覆盖层可热生效）。
        "enabled": False,
        # NAS 上的 kw-qq-music-api（内网 10.10.10.0/24 免 key）
        "api_base": "http://10.10.10.2:3003",
        # 源策略：按序尝试、首源失败回落（缺省 = 代码 _DEFAULT_SOURCES）。
        # 排除 bilibili（headers 无通道必 403）与 migu（copyrightId 回传链
        # 复杂化 spike，需要时在此加）。
        "sources": ["kuwo", "netease", "qq"],
        # 音质固定 320k（消费铁律 ≤320k mp3，代码常量 _QUALITY 不可配）
    },
    "wakeup": {
        # 自定义唤醒词列表（英文字母要全小写）
        "keywords": [
            "你好小智",
            "小智小智",
            "hi open claw",
            "你好龙虾",
            "龙虾你好",
            "你好小黑",
            "小黑你好",
            "你好小爪",
            "小爪你好",
            # 音乐生态点歌触发（V2）：与 WAKEWORD_ROUTES 条目配套；纯中文 >=4 字
            "我要点歌",
            "帮我点歌",
        ],
        # 静音多久后自动退出唤醒（秒）
        "timeout": 20,
        # 语音识别结果回调
        "before_wakeup": before_wakeup,
        # 退出唤醒时的提示语（设置为空可关闭）
        "after_wakeup": after_wakeup,
    },
    "kws": {
        # 唤醒词置信度加成（越高越难误触发，越低越灵敏）
        "keywords_score": 2.0,
        # 唤醒词检测阈值（越低越灵敏，越高越难触发）
        "keywords_threshold": 0.2,
        # 唤醒词检测时的最小静默时长（ms），静默超过该时长则判定为说完
        "min_silence_duration": 480,
    },
    "vad": {
        # 语音检测阈值（0-1，越小越灵敏）
        "threshold": 0.10,
        # 最小语音时长（ms）
        "min_speech_duration": 250,
        # 最小静默时长（ms）
        "min_silence_duration": 500,
    },
    "audio_input": {
        # Input gain multiplier before VAD/KWS/ASR. Use 1.0 to disable.
        "gain": 1.0,
        # 设备侧采集声道数（LX06 六麦 = 6）。bridge 会自动同步到 native 的
        # AUDIO_INPUT_CHANNELS；改成 1 即一键回退单声道（client 无需重编）。
        "channels": 6,
        # 降声道策略：
        #   strongest = 滑动窗口内最强单麦 + 迟滞（默认）
        #   average   = 等权平均
        #   fixed     = 固定单声道（见 fixed_channel）
        "channel_strategy": "strongest",
        # fixed 策略使用的声道号（0-5 有信号，6/7 恒零）
        "fixed_channel": 0,
        # 录音看门狗：连续无音频帧达到该秒数即「先停后启」
        # （client 录音读循环 500ms 无数据即退出，阈值须显著小于 3s）
        "watchdog_seconds": 1.0,
    },
    "inject": {
        # PC 语音注入模块总开关（ticket 07）。默认关闭：
        #   - 关闭时不启动数据面中继 :9093，控制/数据面端点返回 503（code=inject_disabled），
        #     主对话路径完全不受影响；GET /api/inject 与 /api/health 仍报告 enabled=false；
        #   - 开启时行为与既有注入实现一致（窗口 = 闸门 + 中继 + 唤醒，三者同开同关）。
        # 环境变量 INJECT_ENABLE 可覆盖本值（1/true/yes/on 开，0/false/no/off 关）。
        "enabled": False,
    },
    "event_plane": {
        # 播放完成事件主源（blocking 播报的解锁判据）：
        #   context = 轮询 player_get_context.info.status（ticket 03 现状，默认）
        #   client  = client 的 playing 事件（playing→开始，idle→结束，paused 不算结束）
        #   native  = 设备原生 play_status_broadcast_event（status 1/0）
        "source": "context",
        # 影子源：与主源并行采集，只记录开始/结束时间线与差异，不改变等待结果。
        # 先比对、确认一致后再把 source 切到新源（迁移可二分）。
        "shadow_sources": ["client"],
        # context 主源轮询间隔（ms）
        "poll_interval_ms": 200,
        # 从未观察到 playing 时，看到空闲后等待多久判「短音频已播完」（ms）
        "start_grace_ms": 1000,
        # native 源的外部拉流命令（ssh ... ubus listen；P0.5 起运行时镜像已含 openssh-client）。
        # 留空 = 不启动 native 源（默认）。启用时需带 dropbear 兼容与 NAT 保活参数，
        # 现网参考值见 deploy/config.py 同段注释。
        "native_cmd": "",
    },
    "turn_trace": {
        # 每轮对话延迟埋点（ticket 11）。默认关闭：关闭时零日志、零行为变化。
        # 开启后每轮只输出一条 [TurnTrace] INFO 数值行（各段 ms / 文案长度 /
        # timeout 与 fallback 标志），绝不输出原话文本。
        # 环境变量 TURN_TRACE_ENABLED 可覆盖本值（1/true/yes/on 开，0/false/no/off 关）。
        "enabled": False,
        # 每轮 summary 全量 JSONL 落 data/turntrace/（B-32/A4）：容器重启不丢
        # 统计口径（≥40 轮 SLO 复核数据源）。只记数值无 PII。环境变量
        # TURN_TRACE_PERSIST 可覆盖。单开 persist（不开 enabled）也会建轮落盘。
        "persist": False,
    },
    "usage_ledger": {
        # 用量记账（P2 ticket 03）：每轮一行 JSONL 落盘到 data/usage/，面板
        # 「用量」页查询。只记账零控制——无限额/断路/切换（2026-10-25 裁决 Q2：
        # 控制面等 2 个月真实账单后再定阈值）。默认关闭（开发/测试不落盘）；
        # 生产 deploy/config.py 置 True。环境变量 USAGE_LEDGER_ENABLED 可覆盖。
        "enabled": False,
    },
    "asr": {
        # 支持 "sense_voice"（默认）、"paraformer"、"fire_red_asr" 或 "doubao"
        "model": "sense_voice",
        # 是否优先使用 INT8 量化模型（仅本地模型生效）
        "int8": True,
        # 可选：显式指定 core/models/ 下的模型目录名（仅本地模型生效）
        # "model_dir": "",
        "doubao": {
            # "standard": 录音文件识别标准版，调用 /submit + /query
            # "flash": 录音文件极速版，调用 /recognize/flash
            "mode": "standard",
            "app_key": "你的 App Key",
            "access_key": "你的 Access Key",
            # 火山 X-Api-Resource-Id：
            # standard 可选：
            #   "volc.bigasr.auc"  - 豆包录音文件识别模型 1.0
            #   "volc.seedasr.auc" - 豆包录音文件识别模型 2.0
            # flash 可选：
            #   "volc.bigasr.auc_turbo" - 录音文件极速版
            "resource_id": "volc.seedasr.auc",
            "language": "",
            "submit_timeout": 10,
            "query_timeout": 10,
            "poll_interval": 0.5,
            "max_wait_seconds": 20,
        },
    },
    "xiaozhi": {
        "OTA_URL": "http://127.0.0.1:8003/xiaozhi/ota/",
        "WEBSOCKET_URL": "ws://127.0.0.1:8000/xiaozhi/v1/",
        "WEBSOCKET_ACCESS_TOKEN": "", #（可选）一般用不到这个值
        "DEVICE_ID": "00:15:5d:44:4a:0c", #（可选）默认自动生成
        "VERIFICATION_CODE": "", # 首次登陆时，验证码会在这里更新
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧"],
        "max_listening_retries": 2,  # 最多连续重新唤醒次数
        "exit_prompt": "再见，主人",
        "continuous_conversation_keywords": ["开启连续对话", "启动连续对话", "我想跟你聊天"]
    },
    # TTS (Text-to-Speech) Configuration
    "tts": {
        # 可选 provider 段落（配合各后端的 tts_provider 使用）：
        # "openai": {            # OpenAI 官方或任意兼容 /v1/audio/speech 的服务
        #     "base_url": "https://api.openai.com/v1",
        #     "api_key": "sk-xxxx",
        #     "model": "gpt-4o-mini-tts",
        #     "voice": "alloy",
        #     "response_format": "mp3",  # 仅 mp3/flac/wav/ogg/pcm 可被音箱播放
        # },
        # "mlx_audio": {         # Apple Silicon 本地的 MLX-Audio 服务
        #     "base_url": "http://host.docker.internal:8000/v1",
        #     "model": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-6bit",
        #     "mode": "voice_design",  # 或 "custom_voice"
        #     "response_format": "wav",
        # },
        "doubao": {
            # 豆包语音合成 API 配置
            # 文档地址: https://www.volcengine.com/docs/6561/1598757?lang=zh
            # 产品地址: https://www.volcengine.com/docs/6561/1871062
            "app_id": "xxxx",         # 你的 App ID
            "access_key": "xxxxxx",       # 你的 Access Key
            "default_speaker": "zh_female_vv_uranus_bigtts",  # 音色 https://www.volcengine.com/docs/6561/1257544?lang=zh
            "audio_format": "pcm",  # 推荐默认值：局域网稳定环境下首音更快、播放更顺
            "stream": True,  # 推荐默认值：边合成边播放，首音延迟更低
        }
    },
    # OpenClaw Configuration
    "openclaw": {
        "url": "ws://127.0.0.1:18789",  # OpenClaw WebSocket 地址
        "token": "your_openclaw_token",  # OpenClaw 认证令牌
        # 输入模式：
        #   - "local_asr": 现有链路，使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 实验链路，唤醒小爱后接管原生 ASR 结果给 OpenClaw
        "input_mode": "local_asr",
        # session_key 格式：agent:<agentId>:<rest>
        #   agentId: OpenClaw 中配置的 Agent ID（默认为 main）
        #   rest:    会话标识，可自由命名，用于区分不同来源/场景 （默认为 open-xiaoai-bridge)
        # 也可在运行时动态切换（下一条消息即刻生效，无需重连）：
        #   app.set_session_key("openclaw", "agent:assistant:open-xiaoai-bridge")
        "session_key": "agent:main:open-xiaoai-bridge",
        "identity_path": "/app/openclaw/identity/device.json",  # 设备身份文件路径；容器部署时建议挂载持久化目录
        "tts_speed": 1.0,  # TTS 语速 (0.5-2.0)，仅豆包 TTS 生效，小爱原生 TTS 不支持调速
        "tts_speaker": "xiaoai",  # "xiaoai" = 小爱原生 TTS；填豆包音色 ID 则用豆包 TTS；不设置则使用 tts.doubao.default_speaker
        # tts_provider: 显式指定 TTS 供应商，覆盖 tts_speaker 的隐式推断。
        #   可选 "xiaoai" / "doubao" / "openai" / "mlx_audio"；不填则保持旧逻辑
        #   （"xiaoai" 音色→小爱原生，其他音色 ID→豆包）。
        #   openai/mlx_audio 需另外配置 tts.openai / tts.mlx_audio 段。
        # "tts_provider": "doubao",
        # 可按 agentId 单独覆盖音色，优先级高于 tts_speaker
        # agentId 来自 session_key，格式为：agent:<agentId>:<rest>
        # 示例：
        # "agent_tts_speakers": {
        #     "assistant": "zh_female_vv_uranus_bigtts",
        #     "xiaomei": "zh_female_shuangkuaisisi_moon_bigtts",
        #     "butler": "xiaoai",
        # },
        "agent_tts_speakers": {},
        "response_timeout": 120,  # 等待 OpenClaw agent 响应的超时时间（秒）
        "exit_keywords": ["退出", "停止", "再见"],  # 退出连续对话的关键词
        # rule_prompt: 用于「自动播放」和「连续对话」场景
        #   - app.send_to("openclaw", text, and_play=True) 会自动追加
        #   - OpenClawConversationController 会自动追加
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        # rule_prompt_for_skill: 用于「Agent 自主播报」场景（方式三）
        #   - app.send_to("openclaw", text)（纯发送）会自动追加
        #   - 告诉 Agent 需要调用 xiaoai-tts skill 来播报，因为服务端不会自动播放
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字，调用 `xiaoai-tts` skill 播报出来。字数控制在300字以内"
    },
    # OpenAI-compatible Service Configuration
    # 可接入 Hermes Agent API Server、OpenAI、Ollama、LM Studio 等兼容 /v1/chat/completions 的服务
    "openai": {
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 统一采用 agent:<agentId>:<rest> 格式，便于 after_wakeup 解析
        "session_key": "agent:default:open-xiaoai-bridge",
        # 可选：把 session_key 作为请求头发给服务端，用于服务端长期记忆作用域。
        # 默认设为 Hermes 的 "X-Hermes-Session-Key"；它只用于长期记忆作用域，
        # chat/completions 仍是无状态（历史仍由 messages 携带），不会重复。
        # 接标准 OpenAI/Ollama/LM Studio 时该头会被忽略（无害），如需彻底关闭可留空。
        "session_header": "X-Hermes-Session-Key",
        "system_prompt": "",
        "temperature": 0.7,
        "max_tokens": 512,
        "history_max_messages": 20,
        "response_timeout": 120,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        # "tts_provider": "doubao",  # 可选 xiaoai/doubao/openai/mlx_audio；不填保持旧逻辑
        "session_tts_speakers": {},
        "exit_keywords": ["退出", "停止", "再见"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送的，他看不到你回复的文字。字数控制在300字以内",
        # 「换个话题」暗号（spec topic-switch-and-memory）：exact=整句精确命中；
        # fuzzy=≤8 字且编辑距离 ≤2（长句永不进模糊通道——防误拦核心门槛，
        # 调整词表勿放宽）。命中即清历史 + 下轮带 X-Session-Action: new 信令 +
        # 直接播过渡语（不发 LLM）。
        "new_topic": {
            "enabled": True,
            "exact": ["我们换个话题"],
            "fuzzy": ["换个话题", "聊点别的", "换个话题吧"],
            "transition": "好的，我们聊点别的。想聊什么？",
        },
        # 对话记忆（spec topic-switch-and-memory）：每轮 JSONL 落 data/memory/
        # （NAS 本地卷）；上下文断档（超 gap_minutes / 刚换话题）时注入最近
        # max_turns 轮摘要。retain_days 到期文件写入时惰性清理。
        "memory": {
            "enabled": True,
            "max_turns": 5,
            "retain_days": 30,
            "gap_minutes": 30,
        },
        "extra_body": {},
    },
    # QwenPaw Configuration
    # 需先启动 QwenPaw: qwenpaw app
    "qwenpaw": {
        "base_url": "http://127.0.0.1:8088",
        # agent_id 仅作为回退：当 session_key 不含 agentId 时才使用。
        # 正常情况下 agentId 直接从 session_key 的第二段解析，无需单独配置。
        "agent_id": "default",
        "user_id": "open-xiaoai-bridge",
        # 输入模式：
        #   - "local_asr": 使用本地 VAD + SherpaASR
        #   - "xiaoai_asr": 接管小爱原生 ASR 结果
        "input_mode": "local_asr",
        # session_key 采用 agent:<agentId>:<sessionId> 格式：
        #   - 第二段 agentId 作为 X-Agent-Id 请求头发给服务端
        #   - 第三段起为 sessionId，作为请求体 session_id 发给服务端
        # 例如 agent:default:open-xiaoai-bridge -> agent=default, session=open-xiaoai-bridge
        "session_key": "agent:default:open-xiaoai-bridge",
        # QwenPaw 当前推荐使用后台任务接口：
        # POST /api/console/chat/task -> GET /api/console/chat/task/{task_id}
        "send_path": "/api/console/chat/task",
        "task_status_path": "/api/console/chat/task/{task_id}",
        # 认证（可选）：非空时默认使用 Authorization 认证头
        "auth_token": "",
        "response_timeout": 120,
        "poll_interval": 0.5,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",
        # "tts_provider": "doubao",  # 可选 xiaoai/doubao/openai/mlx_audio；不填保持旧逻辑
        "session_tts_speakers": {},
        "exit_keywords": ["退出", "停止", "再见"],
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "注意：这条消息是主人通过小爱音箱发送给 QwenPaw 的，他看不到你回复的文字。字数控制在300字以内",
    },
}
