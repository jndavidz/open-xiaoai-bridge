"""双指令表执行器：kws 免唤醒与小爱截胡共用的步骤序列执行逻辑。

P0 ticket 03 起 `str` 播报走 `speaker.announce`（双闸门 + 词表安全检查 +
完成等待 + finally 归还），本模块不手工编排闸门。词表检查沿用默认拒绝：
指令确认文案属部署配置，若与词表冲突应在改词表/文案时显式处置，
而不是静默豁免。
"""

from core.commands import hass


async def run_steps(speaker, steps):
    """执行指令表的一步序列：str=播报，("ha",…)=HA 调用，callable=任意函数。

    步骤按声明顺序执行；str 步骤经 announce 双闸门，词表命中或播放失败
    即异常上抛、后续步骤不执行（默认拒绝）；HA 调用失败只记日志、不中断序列。
    """
    for step in steps:
        if isinstance(step, str):
            async with speaker.announce(
                text=step,
                reason="run_steps TTS",
                wait=False,  # play(text) 阻塞式内含等完成
                gate_seconds=300,  # 长指令序列兜底：远大于单条 TTS 实测时长，防闸门悬挂
            ):
                await speaker.play(text=step)
        elif isinstance(step, tuple) and step[0] == "ha":
            await hass.hass_action(step[1], step[2], step[3] if len(step) > 3 else None)
        elif callable(step):
            await step(speaker)
