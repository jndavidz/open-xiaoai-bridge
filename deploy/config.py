# ============================================================
# 家庭定制配置 —— jndavidz 部署版
# 原则：密钥一律走环境变量（见 .env），本文件可安全提交
# 修改本文件保存即热重载（约 1 秒生效），无需重启容器
# ============================================================
import asyncio
import os

import aiohttp

from core.utils.logger import logger  # noqa: E402  规范：禁止裸 print
from core.ref import get_kws  # noqa: E402  T7.6「停止聆听」需直接操作 KWS 单例

# ---------- 环境变量 ----------
HA_BASE_URL = os.environ.get("HA_BASE_URL", "http://10.10.10.2:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ---------- Home Assistant REST 调用 ----------
async def hass_action(domain: str, service: str, data: dict | None = None):
    """调用 HA 服务（异步非阻塞）。失败只记日志不打断语音流程。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{HA_BASE_URL}/api/services/{domain}/{service}",
                headers={
                    "Authorization": f"Bearer {HA_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=data or {},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 300:
                    logger.error(f"[home] HA {domain}.{service} -> HTTP {resp.status}")
                else:
                    logger.info(f"[home] HA {domain}.{service} OK")
                return resp.status
    except Exception as e:
        logger.error(f"[home] HA {domain}.{service} failed: {e}")
        return None


# ---------- 指令动作构造器（声明式：一行声明一条指令） ----------
def ha_script(name: str):
    """一行声明调用一个 HA script（如 hifi_mode / music_stop / music_next）。"""
    return ("ha", "script", "turn_on", {"entity_id": f"script.{name}"})


def ha_switch(entity_id: str):
    """一行声明打开一个 HA switch（如 WOL 网络唤醒开关 switch.nuc_hifi_wol）。"""
    return ("ha", "switch", "turn_on", {"entity_id": entity_id})


# ---------- 网络层原子控制：KODI 启动（经 HA androidtv 集成走 ADB） ----------
# 前提（缺一不可，均不属本文件职责）：
#   ① Vidda 电视已开机——Vidda 无 WOL，关机即网络全断，ADB 不可达；开机属红外/Phase E；
#   ② 电视已安装 KODI（org.xbmc.kodi）——2026-08-29 联机实测已确认安装，主 Activity=org.xbmc.kodi/.Splash
#      （Splash 为 LAUNCHER 入口，启动后接管到 .Main；.StartActivity/.Main 均非 LAUNCHER，勿用）。
# 满足前提后，HA androidtv 集成自动 ADB 重连，本调用即时生效；否则调用失败（仅记日志、不崩溃）。
async def kodi_launch(speaker):
    """经 HA androidtv 集成向电视发 ADB 指令启动 KODI（STRM 直链播放入口）。"""
    await hass_action(
        "androidtv", "adb_command",
        {"entity_id": "media_player.vidda_tv",
         "command": "am start -n org.xbmc.kodi/.Splash"},
    )


async def _run_steps(speaker, steps):
    """执行指令表的一步序列：str=播报，("ha",…)=HA 调用，callable=任意函数。
    kws 免唤醒（DIRECT_COMMANDS）与小爱截胡（XIAOAI_COMMANDS）共用同一执行逻辑。"""
    for step in steps:
        if isinstance(step, str):
            await speaker.play(text=step)
        elif isinstance(step, tuple) and step[0] == "ha":
            await hass_action(step[1], step[2], step[3] if len(step) > 3 else None)
        elif callable(step):
            await step(speaker)


# ---------- 学科辅导导师人设（「你好老师」专属会话） ----------
# 四年级 MVP：苏格拉底式引导，语音友好，场景限定
TUTOR_SYSTEM_PROMPT = (
    "你是'老师'，一位耐心的小学四年级学科辅导老师，通过音箱与学生语音对话。"
    "学生是9-10岁的孩子。规则："
    "1.苏格拉底式引导——先问学生已经知道什么，一步步引导他自己得出答案，绝不直接报答案；"
    "2.每次回复不超过3句话，口语化、简短、适合语音播报，不用任何markdown、列表符号和表情；"
    "3.只聊功课：口算与数学概念、语文生词古诗阅读、英语单词互译、课本百科问答；"
    "4.超出范围的话题温和地说'这个问题我们下课再聊，问点功课吧'；"
    "5.答对要具体表扬，答错给提示而不是纠正。"
)


# ---------- 免唤醒指令表（source=="kws" 命中即执行，不进对话） ----------
# 动作元素类型（与 XIAOAI_COMMANDS 共用 _run_steps 执行）：
#   str                          -> 音箱 TTS 播报（先播的 str 充当"提示音掩盖延迟"）
#   ("ha", domain, service, {})  -> 调用 HA 服务（用 ha_script / ha_switch 构造器声明）
#   callable(speaker)            -> 任意异步函数
# 说明：所有设备动作统一走 HA script（在 HA 侧维护具体逻辑），
#       本文件只认 script 名，与实体/集成解耦。
DIRECT_COMMANDS = {
    # --- 调试 ---
    "测试模式": ["桥接正常，家庭中枢在线"],
    # --- HIFI 场景（HA script 内部：WOL 唤醒 NUC -> LMS 播放 -> 完成后经 :9092 播报）---
    "音乐模式": ["正在开启高保真模式", ha_script("hifi_mode")],
    "高保真模式": ["正在开启高保真模式", ha_script("hifi_mode")],
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
    "开启高保真": ["正在开启高保真模式", ha_script("hifi_mode")],
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
    "HiFi模式": ["正在开启高保真模式", ha_script("hifi_mode")],
    "Hi-Fi模式": ["正在开启高保真模式", ha_script("hifi_mode")],
    # 网络层：KODI 启动（ADB 控电视，不依赖红外）。需电视开机 + KODI 已装（见 kodi_launch 注释）。
    # 走小爱截胡：原生小爱无法 ADB 启动电视端 KODI，属跨系统编排，命中即截胡。
    "打开Kodi": ["正在打开 Kodi", kodi_launch],
    "启动Kodi": ["正在打开 Kodi", kodi_launch],
    "打开KODI": ["正在打开 Kodi", kodi_launch],
}


async def before_wakeup(speaker, text, source, app):
    """
    唤醒边界路由：
      - source == "kws"   ：免唤醒短语命中 DIRECT_COMMANDS 则执行并结束；否则视为 AI 唤醒词进入对话
      - source == "xiaoai"：命中 XIAOAI_COMMANDS（原生小爱做不到的跨系统编排）则截胡执行；
                            未命中放行原生小爱全权处理
    """
    if source == "kws":
        key = (text or "").strip()
        steps = DIRECT_COMMANDS.get(key)
        if steps is not None:
            await _run_steps(speaker, steps)
            return None  # 执行完毕，不进入 AI 连续对话

        # T7.6「停止聆听」常驻隐私开关：硬件麦克风静音 + KWS 持久停止分析。
        # 一旦停止，语音通道自关——唯一恢复路径是 :9092 的 POST /api/audio_input。
        if key == "停止聆听":
            try:
                await speaker.set_mic(False)
            except Exception as e:  # 静音失败绝不应阻断播报
                logger.warning(f"[home] set_mic(False) on 停止聆听 failed: {e}")
            kws = get_kws()
            if kws:
                kws.disable_listening()
            logger.info("[home] 停止聆听: mic muted + KWS listening disabled")
            await speaker.play(text="已停止聆听")
            return None

        if "你好老师" in key:
            # 切入导师会话：独立 session_key（历史隔离）+ 独立人设覆盖
            app.set_openai_session_key("agent:tutor:home")
            app.set_openai_system_prompt(TUTOR_SYSTEM_PROMPT)
            await speaker.play(text="老师在，请讲")
            return "openai"  # 进入 DeepSeek 连续对话（导师人设）

        if "贾维斯" in key:
            # 切回贾维斯：显式复位 session_key 并清除导师人设覆盖
            app.set_openai_session_key("agent:javis:home")
            app.set_openai_system_prompt(None)
            await speaker.play(text="我在")
            return "openai"  # 进入 DeepSeek 连续对话
        return None

    if source == "xiaoai":
        # 拦截"原生小爱做不到的跨系统编排口令"（见 XIAOAI_COMMANDS 边界规则）。
        # 未命中则放行原生小爱——米家设备控制等口语不受影响。
        key = (text or "").strip()
        steps = XIAOAI_COMMANDS.get(key)
        if steps is not None:
            # T7.3 执行前清场：压掉原生小爱可能的重叠应答。
            # 用 stop_device_audio()（只停播放、可逆、无 abort/sleep），
            # 不照抄 oxa-server 的 interrupt_xiaoai()（abort+sleep(2) 与本 fork 冲突）。
            # 净安全：本 fork 若原生不重叠应答，此调用为空操作；若重叠则被压掉。
            try:
                await speaker.stop_device_audio()
            except Exception as e:  # 清场失败绝不应阻断编排执行
                logger.warning(f"[home] stop_device_audio before xiaoai intercept: {e}")
            await _run_steps(speaker, steps)
            return None  # 截胡执行完毕，不进原生小爱
        return None  # 未命中 → 放行原生小爱全权处理

    return None


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
        # 唤醒词置信度加成（越高越难误触发）；实测误触发再上调
        "keywords_score": 2.0,
        # 检测阈值（越低越灵敏）
        "keywords_threshold": 0.2,
        # 判定说完的最小静默时长（ms）
        "min_silence_duration": 480,
    },
    "vad": {
        # share 项目客厅环境实战值 0.3（默认 0.10 过敏易误触发），实测微调
        "threshold": 0.3,
        "min_speech_duration": 250,
        "min_silence_duration": 500,
    },
    "audio_input": {
        # 麦克风增益 1.0-8.0；LX06 收音偏弱再逐步上调，过高削波
        "gain": 1.0,
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
    },
}
