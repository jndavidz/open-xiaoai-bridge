"""纯动作指令的短提示音（§1.5 裁决：加「收到」提示音，不用 TTS）。

指令表里「停止/暂停/继续/上下首/音量」这类条目第一步就是 HA 脚本、没有播报文案，
说完完全没有反馈，用户容易以为没响应。本模块在**整条序列没有文本步骤**时补一个
约 0.3s 的提示音（复用 `assets/sounds/tts_notify.mp3`），语义即「收到」。

- 纯波形、无词，不触发 KWS 词表自检；也不需要 announce 闸门；
- 失败只记日志：提示音是观感增强，绝不能因此让指令失败；
- 开关：config `commands.ack_sound`（默认 true，deploy 显式写 true）。
"""

from pathlib import Path

from core.utils.logger import logger

_ACK_SOUND_PATH = Path(__file__).resolve().parents[1] / "assets" / "sounds" / "tts_notify.mp3"

_pcm_cache: bytes | None = None
_loaded = False


def _load_pcm() -> bytes | None:
    global _pcm_cache, _loaded
    if _loaded:
        return _pcm_cache
    _loaded = True
    try:
        import open_xiaoai_server

        _pcm_cache = open_xiaoai_server.decode_audio(
            _ACK_SOUND_PATH.read_bytes(), format="mp3", sample_rate=24000
        )
    except Exception as exc:  # noqa: BLE001 - 提示音加载失败不影响指令
        logger.warning(f"[commands] ack sound load failed: {exc}")
        _pcm_cache = None
    return _pcm_cache


def ack_enabled() -> bool:
    """读 `commands.ack_sound`，配置不可用时按开（deploy 默认开）。"""

    try:
        from core.utils.config import ConfigManager

        return bool(ConfigManager.instance().get_app_config("commands.ack_sound", True))
    except Exception:  # noqa: BLE001 - 测试/早期启动没有配置实例
        return True


async def play_ack(speaker) -> None:
    pcm = _load_pcm()
    if not pcm:
        return
    try:
        await speaker.play(buffer=pcm)
    except Exception as exc:  # noqa: BLE001 - 提示音失败不阻断
        logger.warning(f"[commands] ack sound play failed: {exc}")
