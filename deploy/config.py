# ============================================================
# 家庭定制配置 —— jndavidz 部署版
# 原则：密钥一律走环境变量（见 .env），本文件可安全提交
# 修改本文件保存即热重载（约 1 秒生效），无需重启容器
#
# 数据面/逻辑面分工（C4/P2 ticket 01 指令层内核化）：
#   本文件只留纯数据 —— 两张指令表 + KWS 词表 + 人设表 + 路由钩子绑定；
#   执行器、HA REST client、动作构造器、清场次序在 core/commands/（随镜像）。
#   路由钩子只是「数据 → 核心执行器」的绑定层，不含执行逻辑，
#   也不再反向 import 核心单例（原 core.ref get_kws 依赖已随内核化移入 core）。
# ============================================================
import os

from core.commands import (
    ha_script,
    ha_switch,
    kodi_launch,
    route_before_wakeup,
)

# ---------- 环境变量 ----------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


# ---------- 会话人设（专属会话表，按声明顺序做子串匹配） ----------
# 学科辅导导师人设（「你好老师」专属会话）：四年级 MVP，苏格拉底式引导，
# 语音友好，场景限定。system_prompt=None 表示显式复位人设覆盖（贾维斯行用）。
TUTOR_SYSTEM_PROMPT = (
    "你是'老师'，一位耐心的小学四年级学科辅导老师，通过音箱与学生语音对话。"
    "学生是9-10岁的孩子。规则："
    "1.苏格拉底式引导——先问学生已经知道什么，一步步引导他自己得出答案，绝不直接报答案；"
    "2.每次回复不超过3句话，口语化、简短、适合语音播报，不用任何markdown、列表符号和表情；"
    "3.只聊功课：口算与数学概念、语文生词古诗阅读、英语单词互译、课本百科问答；"
    "4.超出范围的话题温和地说'这个问题我们下课再聊，问点功课吧'；"
    "5.答对要具体表扬，答错给提示而不是纠正。"
)

PERSONAS = {
    "你好老师": {
        "session_key": "agent:tutor:home",
        "system_prompt": TUTOR_SYSTEM_PROMPT,
        "intro": "老师在，请讲",
        "backend": "openai",
    },
    "贾维斯": {
        "session_key": "agent:javis:home",
        "system_prompt": None,  # 切回贾维斯：显式复位导师人设覆盖
        "intro": "我在",
        "backend": "openai",
    },
}


# ---------- 免唤醒指令表（source=="kws" 命中即执行，不进对话） ----------
# 动作元素类型（与 XIAOAI_COMMANDS 共用 core/commands run_steps 执行）：
#   str                          -> 音箱 TTS 播报（先播的 str 充当"提示音掩盖延迟"）
#   ("ha", domain, service, {})  -> 调用 HA 服务（用 ha_script / ha_switch 构造器声明）
#   callable(speaker)            -> 任意异步函数
# 说明：所有设备动作统一走 HA script（在 HA 侧维护具体逻辑），
#       本表只认 script 名，与实体/集成解耦。
DIRECT_COMMANDS = {
    # --- 调试 ---
    "测试模式": ["桥接正常，家庭中枢在线"],
    # --- HIFI 场景（HA script 内部：WOL 唤醒 NUC -> LMS 播放 -> 完成后经 :9092 播报）---
    "音乐模式": ["正在准备客厅音响", ha_script("hifi_mode")],
    "高保真模式": ["正在准备客厅音响", ha_script("hifi_mode")],
    # --- 音乐播放控制（对应 HA script，内部调 Music Assistant / LMS）---
    "停止音乐": [ha_script("music_stop")],
    "暂停音乐": [ha_script("music_stop")],
    "继续播放": [ha_script("music_play")],
    "下一首歌曲": [ha_script("music_next")],
    "上一首歌曲": [ha_script("music_prev")],
    "音乐大声点": [ha_script("music_vol_up")],
    "音乐小声点": [ha_script("music_vol_down")],
    # --- 同义词扩展（T7.5，纯本地复用现有 HA script，零误吞；词均 >=4 字降误触）---
    "大点声音": [ha_script("music_vol_up")],
    "声音大一点": [ha_script("music_vol_up")],
    "调大音量": [ha_script("music_vol_up")],
    "小点声音": [ha_script("music_vol_down")],
    "声音小一点": [ha_script("music_vol_down")],
    "调小音量": [ha_script("music_vol_down")],
    "切换下一首": [ha_script("music_next")],
    "切换上一首": [ha_script("music_prev")],
    "继续放音乐": [ha_script("music_play")],
    "开始播放": [ha_script("music_play")],
    "暂停播放": [ha_script("music_stop")],
    "先暂停一下": [ha_script("music_stop")],
    "停止播放音乐": [ha_script("music_stop")],
    "开启高保真": ["正在准备客厅音响", ha_script("hifi_mode")],
}


# ---------- 小爱口令截胡表（source=="xiaoai" 分支） ----------
# 与 DIRECT_COMMANDS（kws 免唤醒）不同：这里是"用户说「小爱同学，XXX」时"，
# 原生小爱做不到、需要跨系统编排的动作。命中即截胡执行、不进原生小爱；
# 未命中则放行原生（before_wakeup 返回 None），保证天气/点歌/米家设备照旧。
# ⚠ 边界规则（plan §4.3 / selection-review §3.1.2）：
#   米家设备控制类口语（「把空调开到 26 度」）绝不在此表——必须放行原生小爱，
#   否则会吞掉原生指令。本表只放"原生做不到的跨系统编排"，且保持最小集，
#   宁可少截不可错吞（边界调优见 runbook T7.5）。
# 首实例"打开电脑"=WOL 唤醒 NUC：原生小爱无此能力，实体名见
# deploy/ha/config-append2.yaml（switch.nuc_hifi_wol，T5.8 实测 24s 上线）。
# 注：「进入影院」等依赖红外执行器的编排暂不入表（RM4 Pro 已取消采购，决策更新②）。
XIAOAI_COMMANDS = {
    "打开电脑": ["正在唤醒客厅电脑", ha_switch("switch.nuc_hifi_wol")],
    # Hi-Fi 模式：复用现有 script.hifi_mode（无需新 HA 实体）；走小爱截胡，
    # 因 KWS 离线模型不识别含英文的「HiFi模式」，改由「小爱同学，Hi-Fi模式」触发
    "HiFi模式": ["正在准备客厅音响", ha_script("hifi_mode")],
    "Hi-Fi模式": ["正在准备客厅音响", ha_script("hifi_mode")],
    # 网络层：KODI 启动（ADB 控电视，不依赖红外）。需电视开机 + KODI 已装
    # （前提与实现见 core/commands/actions.py kodi_launch 注释）。
    # 走小爱截胡：原生小爱无法 ADB 启动电视端 KODI，属跨系统编排，命中即截胡。
    "打开Kodi": ["正在打开 Kodi", kodi_launch],
    "启动Kodi": ["正在打开 Kodi", kodi_launch],
    "打开KODI": ["正在打开 Kodi", kodi_launch],
}
# ⚠ 2026-10-26 真机语音实测（6 词仅 1 中）：原生 ASR 对含拉丁字母的键转写不稳——
#   HiFi模式→「哈哈模式」、Hi-Fi模式→「阿尔法模式」、打开KODI→「打开CODY」；
#   且「打开Kodi」曾被转成「打开电脑」**误命中执行（WOL 两次）**。修法（中文触发词 /
#   ASR 别名键 / 匹配归一化）待裁决，见 ws: docs/plan/pending-followups.md §5。

# ---------- 指令别名表（B-31，可选）----------
# {别名: 正式键}——正式键必须存在于上面两张表之一；匹配经归一化
# （core/commands/matching.py：全角→半角、去标点空白、拉丁小写）。
# 表空 = 现状逐字等价。填充须用户裁决（宁可少截不可错吞）：
# 候选样例（转写抖动实测）：{"打开CODY": "打开Kodi", "阿尔法模式": "HiFi模式"}
# ⚠ 拼音级模糊容错不在本表机制内——那属匹配策略裁决（W4），未拍板前不加。
COMMAND_ALIASES: dict = {}


async def before_wakeup(speaker, text, source, app):
    """唤醒边界路由钩子：纯数据绑定，分发与执行逻辑在 core/commands/router.py。

      - source == "kws"   ：免唤醒短语命中 DIRECT_COMMANDS 则执行并结束；
                            「停止聆听」走常驻隐私开关（T7.6，core 侧）；
                            人设子串命中则切会话人设进入 AI 连续对话
      - source == "xiaoai"：命中 XIAOAI_COMMANDS（原生小爱做不到的跨系统编排）
                            则先清场再截胡执行；未命中放行原生小爱全权处理
    """
    return await route_before_wakeup(
        speaker, text, source, app,
        direct_table=DIRECT_COMMANDS,
        xiaoai_table=XIAOAI_COMMANDS,
        personas=PERSONAS,
        aliases=COMMAND_ALIASES,
    )


async def after_wakeup(speaker, source=None, session_key=None):
    """退出连续对话。保持安静更自然，仅调试时可打开播报。"""
    # if source == "openai":
    #     await speaker.play(text="随时叫我")
    return None


APP_CONFIG = {
    # 固定设备 ID，避免 ConfigManager.reload_app_config() 在每次热重载时
    # 因 DEVICE_ID 缺失而回写 config.py 源文件（导致 mtime 抖动 → 文件监听
    # 误判变更 → 每秒 reload 死循环、刷屏淹没真实日志）。值需为合法 MAC 格式。
    "xiaozhi": {
        "DEVICE_ID": "aa:bb:cc:dd:ee:ff",
    },
    "wakeup": {
        # KWS 总词表 = AI 唤醒词 + 全部免唤醒短语（动态编译为拼音，中文即可）
        # 注意：短语 >=4 字更稳；误触发/失灵时优先调 kws/vad 段参数
        "keywords": [
            "你好贾维斯",
            "你好老师",
            "音乐模式",
            "高保真模式",
            "停止音乐",
            "暂停音乐",
            "继续播放",
            "下一首歌曲",
            "上一首歌曲",
            "音乐大声点",
            "音乐小声点",
            "测试模式",
            "停止聆听",  # T7.6 隐私开关口令（KWS 命中后硬件静音 + KWS 持久停止；仅 :9092 /api/audio_input 可恢复）
            # --- T7.5 同义词扩展（与 DIRECT_COMMANDS 新增键一致）---
            "大点声音", "声音大一点", "调大音量",
            "小点声音", "声音小一点", "调小音量",
            "切换下一首", "切换上一首",
            "继续放音乐", "开始播放", "暂停播放", "先暂停一下", "停止播放音乐",
            "开启高保真",
        ],
        "timeout": 20,
        "before_wakeup": before_wakeup,
        "after_wakeup": after_wakeup,
    },
    "kws": {
        # 唤醒词置信度加成（越高越难误触发）
        # 2026-09-24 收紧：2.0 -> 3.0（incident §5 建议 3）
        # 依据：2026-09-17 自触发回环事故中环境音误命中（score 阈值过松）
        "keywords_score": 3.0,
        # 检测阈值（越低越灵敏）
        # 2026-09-24 收紧：0.2 -> 0.35（incident §5 建议 3，建议区间 0.35~0.4 取下沿）
        "keywords_threshold": 0.35,
        # 判定说完的最小静默时长（ms）
        "min_silence_duration": 480,
    },
    "vad": {
        # share 项目客厅环境实战值 0.3（默认 0.10 过敏易误触发）
        # 2026-09-24 收紧：0.3 -> 0.4（incident §5 建议 3）
        "threshold": 0.4,
        "min_speech_duration": 250,
        "min_silence_duration": 500,
    },
    "audio_input": {
        # 麦克风增益 1.0-8.0；LX06 收音偏弱再逐步上调，过高削波
        "gain": 1.0,
        # 采集声道数（六麦=6）。bridge 自动同步到 native 的 AUDIO_INPUT_CHANNELS；1=回退单声道
        "channels": 6,
        # 降声道策略：strongest（最强麦+迟滞）/ average（等权平均）/ fixed（固定声道）
        "channel_strategy": "strongest",
        "fixed_channel": 0,
        # 录音看门狗：连续无帧秒数（client 500ms 无数据即退出读循环）
        "watchdog_seconds": 1.0,
    },
    "inject": {
        # PC 语音注入模块总开关（默认关闭）：关闭时不监听 :9093，注入端点 503；
        # 开启时窗口 = 闸门 + 中继 + 唤醒，三者同开同关。
        # 环境变量 INJECT_ENABLE 可覆盖（1/true/yes/on 开，0/false/no/off 关）。
        "enabled": False,
    },
    "event_plane": {
        # 播放完成事件主源：context（轮询 player_get_context）/
        # client（playing 事件）/ native（设备原生 play_status_broadcast_event）。
        # 生产已切 native（P0.5 票 B-17 转正，2026-10-26；观察期至 11-09，
        # 回退 = 改回 "context" 热重载即生效）
        "source": "native",
        # 影子源：并行采集、只记录时间线与差异，不改变阻塞播报行为
        "shadow_sources": ["client", "native"],
        "poll_interval_ms": 200,
        "start_grace_ms": 1000,
        # native 拉流命令（生产已启用，compose 已含 openssh-client + 密钥只读挂载
        # /app/keys/lx06-ir；dropbear 兼容 + NAT 保活。一行回滚 = 置空 + source 回 context）
        "native_cmd": "ssh -i /app/keys/lx06-ir/id_rsa -o HostKeyAlgorithms=+ssh-rsa "
                      "-o PubkeyAcceptedAlgorithms=+ssh-rsa -o ServerAliveInterval=30 "
                      "-o ServerAliveCountMax=3 -o StrictHostKeyChecking=yes "
                      "-o UserKnownHostsFile=/app/keys/lx06-ir/known_hosts "
                      "root@10.10.10.20 ubus listen",
    },
    "turn_trace": {
        # 每轮延迟埋点（ticket 11）：默认关闭；开启后每轮一条 [TurnTrace] INFO
        # 数值行（各段 ms / 长度 / 标志），不含原话文本。
        # 环境变量 TURN_TRACE_ENABLED 可覆盖（1/true/yes/on 开，0/false/no/off 关）。
        "enabled": False,
        # summary 全量 JSONL 落 data/turntrace/（B-32/A4）：容器 recreate 不再丢
        # 轮次数据（≥40 轮 SLO 复核数据源）。TURN_TRACE_PERSIST 可覆盖。
        "persist": True,
    },
    "usage_ledger": {
        # 用量记账（P2 ticket 03）：每轮一行 JSONL 写入 data/usage/（compose 卷内），
        # 面板「用量」页按天/模型/后端/会话键查询。只记账零控制——无限额/断路/
        # 切换（2026-10-25 裁决 Q2：控制面等 2 个月真实账单后再定阈值）。
        # 环境变量 USAGE_LEDGER_ENABLED 可覆盖；目录可经 USAGE_LEDGER_DIR 重定向。
        #
        # 流式轮的真实 token：上游须在 SSE 末帧报 usage 才有真实值，否则走
        # 字符估算列。请求侧开关（provider 兼容性自负，个别网关拒未知参数会
        # 400）在 openai.extra_body 里加一行：
        #   "extra_body": {"stream_options": {"include_usage": True}},
        "enabled": True,
    },
    "asr": {
        # input_mode=xiaoai_asr 时不加载本地 ASR 大模型（N3060 跑不动）
        "model": "sense_voice",
        "int8": True,
    },
    "xiaoai": {
        "continuous_conversation_mode": True,
        "exit_command_keywords": ["停止", "退下", "退出", "下去吧"],
        "max_listening_retries": 2,
        "exit_prompt": "再见",
        "continuous_conversation_keywords": ["开启连续对话", "我想跟你聊天"],
    },
    "openai": {
        # DeepSeek（OpenAI 兼容）
        "base_url": "https://api.deepseek.com/v1",
        "api_key": DEEPSEEK_API_KEY,  # 环境变量注入，勿写死
        "model": "deepseek-chat",
        # 关键：接管小爱原生 ASR 结果（无需本地 ASR 模型，NAS 零压力）
        "input_mode": "xiaoai_asr",
        "session_key": "agent:javis:home",
        "session_header": "",  # DeepSeek 不需要，留空关闭
        "system_prompt": (
            "你是家庭智能助手贾维斯，运行在客厅音箱上。回答必须口语化、简洁，"
            "控制在100字以内，不要使用任何 markdown 格式、列表符号和表情。"
        ),
        "temperature": 0.7,
        "max_tokens": 300,
        "history_max_messages": 20,
        "response_timeout": 120,
        "tts_speed": 1.0,
        "tts_speaker": "xiaoai",  # 小爱原生 TTS，零配置
        "session_tts_speakers": {},
        "exit_keywords": ["退出", "停止", "再见"],
        # 工具层（Agent 1.5 / 阶段 1.5）：function calling 回环。默认开启；
        # 仅 chat_completions 风格（DeepSeek）注入 tools。只读工具见 core/tools/。
        "tool_calls_enabled": True,
        "max_tool_rounds": 3,
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "",
        "extra_body": {},
        # P1-05 灰度转正：流式默认开（2026-10-26 真机窗口验证：首音 ~3s vs 基线 4.8s、
        # 边生成边播、打断无半句残留）。回退 = runtime-overrides 一行 stream:false 热重载
        # （非流式 = 单句序列 = 与改前逐字等价，B-22 回退路径）。首音 2.0s SLO 达标依赖
        # 豆包流式 TTS 凭证（暂缓，见 ws: docs/plan/pending-followups.md）。
        "stream": True,
        # 「换个话题」暗号（spec topic-switch-and-memory）：exact=整句精确命中；
        # fuzzy=≤8 字且编辑距离 ≤2（长句永不进模糊通道——防误拦核心门槛，
        # 调整词表勿放宽）。命中即清历史 + 下轮带 X-Session-Action: new 信令 +
        # 直接播过渡语（不发 LLM）。transition 文案须避开词表（命中会被
        # announce 拒绝并留可见日志——B-30 起过渡语经旁路闸门）。
        "new_topic": {
            "enabled": True,
            "exact": ["我们换个话题"],
            "fuzzy": ["换个话题", "聊点别的", "换个话题吧"],
            "transition": "好的，我们聊点别的。想聊什么？",
        },
        # 对话记忆（spec topic-switch-and-memory）：每轮 JSONL 落 data/memory/
        # （NAS 持久卷）；上下文断档（超 gap_minutes / 刚换话题）时注入最近
        # max_turns 轮摘要。retain_days 到期文件写入时惰性清理。
        "memory": {
            "enabled": True,
            "max_turns": 5,
            "retain_days": 30,
            "gap_minutes": 30,
        },
    },
}
