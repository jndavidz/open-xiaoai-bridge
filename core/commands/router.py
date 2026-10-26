"""唤醒边界路由：kws 免唤醒 / 小爱截胡的默认分发逻辑（C4/P2 ticket 01）。

config 保留路由钩子（before_wakeup）并把两张指令表 + 人设表作为数据传入；
执行、清场、停止聆听等核心单例操作全部收敛在本包，config 不再反向 import。

与原 deploy/config.py 内联实现的语义一一对应（P2 ticket 01 逐条迁移零变化）：
  - source == "kws"   ：短语命中 direct_table → 执行并返回 None（不进对话）；
                        「停止聆听」→ 常驻隐私开关；人设子串命中 → 切会话人设
                        并返回其 backend（进入连续对话）；否则 None。
  - source == "xiaoai"：命中 xiaoai_table → 先清场压掉原生重叠应答再截胡执行，
                        返回 None（不进原生小爱）；未命中 → None（放行原生小爱）。
"""

from core.commands.executor import run_steps
from core.commands.matching import normalize_command_text, resolve_command_key
from core.ref import get_kws
from core.utils.logger import logger

STOP_LISTENING_KEYWORD = "停止聆听"


async def clear_device_audio(speaker):
    """T7.3 执行前清场：压掉原生小爱可能的重叠应答。

    用 stop_device_audio()（只停播放、可逆、无 abort/sleep），
    不照抄 oxa-server 的 interrupt_xiaoai()（abort+sleep(2) 与本 fork 冲突）。
    净安全：本 fork 若原生不重叠应答，此调用为空操作；若重叠则被压掉。
    """
    try:
        await speaker.stop_device_audio()
    except Exception as e:  # 清场失败绝不应阻断编排执行
        logger.warning(f"[commands] stop_device_audio before xiaoai intercept: {e}")


async def stop_listening(speaker):
    """T7.6「停止聆听」常驻隐私开关：硬件麦克风静音 + KWS 持久停止分析。

    一旦停止，语音通道自关——唯一恢复路径是 :9092 的 POST /api/audio_input。
    """
    try:
        await speaker.set_mic(False)
    except Exception as e:  # 静音失败绝不应阻断播报
        logger.warning(f"[commands] set_mic(False) on 停止聆听 failed: {e}")
    kws = get_kws()
    if kws:
        kws.disable_listening()
    logger.info("[commands] 停止聆听: mic muted + KWS listening disabled")
    await speaker.play(text="已停止聆听")
    return None


async def route_before_wakeup(
    speaker,
    text,
    source,
    app,
    direct_table=None,
    xiaoai_table=None,
    personas=None,
    aliases=None,
):
    """默认唤醒边界路由。表/人设由 config 钩子以数据传入（热重载即生效）。

    personas：{命中子串: {"session_key", "system_prompt", "intro", "backend"}}，
    按声明顺序做子串匹配，先命中先路由；system_prompt=None 表示显式复位人设覆盖。
    aliases：{别名: 正式键}（B-31，可选）——指令表键的归一化 + 别名解析
    （core/commands/matching.py）；表空/参数缺省 = 现状逐字等价。
    """
    key = (text or "").strip()

    if source == "kws":
        steps_key = resolve_command_key(text, direct_table or {}, aliases)
        steps = (direct_table or {}).get(steps_key) if steps_key else None
        if steps is not None:
            await run_steps(speaker, steps)
            return None  # 执行完毕，不进入 AI 连续对话

        if normalize_command_text(key) == normalize_command_text(STOP_LISTENING_KEYWORD):
            return await stop_listening(speaker)

        for word, persona in (personas or {}).items():
            if word in key:
                backend = persona.get("backend", "openai")
                # session_key 走泛化门面（B-28 票 03：不再只认 openai）；
                # system_prompt 是 OpenAI 系专属会话概念（历史隔离 + 人设覆盖），
                # 其他后端只做人设路由不写 openai 会话态。session_key 缺省 = 纯路由条目。
                if persona.get("session_key"):
                    app.set_session_key(backend, persona["session_key"])
                    if backend == "openai":
                        app.set_openai_system_prompt(persona.get("system_prompt"))
                intro = persona.get("intro")
                if intro:
                    # B-28 票 01：intro 经 announce（词表检查默认拒绝）——人设开场白
                    # 同属"用户配置的 TTS 文案"，命中 KWS 词表就该被拦下并留可见日志，
                    # 而不是播出去冒自触发风险（incident-kws-self-trigger-loop §5）。
                    async with speaker.announce(
                        text=intro,
                        reason="wakeup persona intro",
                        wait=False,  # play(text) 阻塞式内含等完成
                        gate_seconds=30,
                    ):
                        await speaker.play(text=intro)
                return backend  # 进入连续对话
        return None

    if source == "xiaoai":
        steps_key = resolve_command_key(text, xiaoai_table or {}, aliases)
        steps = (xiaoai_table or {}).get(steps_key) if steps_key else None
        if steps is not None:
            await clear_device_audio(speaker)
            await run_steps(speaker, steps)
            return None  # 截胡执行完毕，不进原生小爱
        return None  # 未命中 → 放行原生小爱全权处理

    return None
