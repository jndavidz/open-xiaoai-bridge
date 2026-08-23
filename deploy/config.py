# ============================================================
# 家庭定制配置 —— jndavidz 部署版
# 原则：密钥一律走环境变量（见 .env），本文件可安全提交
# 修改本文件保存即热重载（约 1 秒生效），无需重启容器
# ============================================================
import asyncio
import os

import aiohttp

from core.utils.logger import logger  # noqa: E402  规范：禁止裸 print

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


# ---------- 免唤醒指令表 ----------
# 动作元素类型：
#   str                          -> 音箱 TTS 播报（先播的 str 充当"提示音掩盖延迟"）
#   ("ha", domain, service, {})  -> 调用 HA 服务
#   callable(speaker)            -> 任意异步函数
# 说明：所有设备动作统一走 HA script（在 HA 侧维护具体逻辑），
#       本文件只认 script 名，与实体/集成解耦。
DIRECT_COMMANDS = {
    # --- 调试 ---
    "测试模式": ["桥接正常，家庭中枢在线"],
    # --- HIFI 场景（HA script 内部：WOL 唤醒 NUC -> LMS 播放 -> 完成后经 :9092 播报）---
    "音乐模式": [
        "正在开启高保真模式",
        ("ha", "script", "turn_on", {"entity_id": "script.hifi_mode"}),
    ],
    "高保真模式": [
        "正在开启高保真模式",
        ("ha", "script", "turn_on", {"entity_id": "script.hifi_mode"}),
    ],
    # --- 音乐播放控制（对应 HA script，内部调 Music Assistant / LMS）---
    "停止音乐": [("ha", "script", "turn_on", {"entity_id": "script.music_stop"})],
    "暂停音乐": [("ha", "script", "turn_on", {"entity_id": "script.music_stop"})],
    "继续播放": [("ha", "script", "turn_on", {"entity_id": "script.music_play"})],
    "下一首歌曲": [("ha", "script", "turn_on", {"entity_id": "script.music_next"})],
    "上一首歌曲": [("ha", "script", "turn_on", {"entity_id": "script.music_prev"})],
    "音乐大声点": [("ha", "script", "turn_on", {"entity_id": "script.music_vol_up"})],
    "音乐小声点": [("ha", "script", "turn_on", {"entity_id": "script.music_vol_down"})],
}


async def before_wakeup(speaker, text, source, app):
    """
    唤醒边界路由：
      - source == "kws"   ：免唤醒短语命中指令表则执行并结束；否则视为 AI 唤醒词进入对话
      - source == "xiaoai"：暂不拦截，原生小爱全权处理（天气/点歌/米家设备照旧）
    """
    if source == "kws":
        key = (text or "").strip()
        steps = DIRECT_COMMANDS.get(key)
        if steps is not None:
            for step in steps:
                if isinstance(step, str):
                    await speaker.play(text=step)
                elif isinstance(step, tuple) and step[0] == "ha":
                    await hass_action(step[1], step[2], step[3] if len(step) > 3 else None)
                elif callable(step):
                    await step(speaker)
            return None  # 执行完毕，不进入 AI 连续对话

        if "贾维斯" in key:
            await speaker.play(text="我在")
            return "openai"  # 进入 DeepSeek 连续对话
        return None

    # source == "xiaoai"：预留。未来在此拦截特定小爱口令（如"进入影院"）
    return None


async def after_wakeup(speaker, source=None, session_key=None):
    """退出连续对话。保持安静更自然，仅调试时可打开播报。"""
    # if source == "openai":
    #     await speaker.play(text="随时叫我")
    return None


APP_CONFIG = {
    "wakeup": {
        # KWS 总词表 = AI 唤醒词 + 全部免唤醒短语（动态编译为拼音，中文即可）
        # 注意：短语 >=4 字更稳；误触发/失灵时优先调 kws/vad 段参数
        "keywords": [
            "你好贾维斯",
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
        "rule_prompt": "注意：将结果处理成纯文字版，不要返回任何 markdown 格式，也不要包含任何代码块，并将字数控制在300字以内",
        "rule_prompt_for_skill": "",
        "extra_body": {},
    },
}
