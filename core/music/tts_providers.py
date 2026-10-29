"""四个 TTS provider 的 focus 适配（B-07）。

统一契约是 `TtsSpeaker`（speak/cancel，各自独立结果），**不用一个布尔成功
合并四家**：

  - doubao / openai / mlx_audio：走既有 `TTSRouter.play`，每次播报独立申请
    一个 Rust 播放令牌；取消 = 按令牌 `stop_tts_playback`（只停自己那段）。
  - xiaoai / 脚本回退：走 `speaker.announce(..., wait=True)`；取消 = 停止设备音频。

依赖都可由调用方注入，测试不需要真实 router/speaker/Rust 扩展。
"""

from __future__ import annotations

import itertools
from typing import Awaitable, Callable

from core.music.tts_focus import FocusToken, SpeakResult

_token_seq = itertools.count(1)


class RouterTtsSpeaker:
    """doubao / openai / mlx_audio：同一路由，不同 provider 名，令牌互相独立。"""

    def __init__(
        self,
        name: str,
        *,
        play: Callable[[str, str, int], Awaitable[None]] | None = None,
        stop: Callable[[int], bool] | None = None,
    ) -> None:
        self.name = name
        self._play = play or self._default_play
        self._stop = stop or self._default_stop
        self._tokens: dict[str, int] = {}

    @staticmethod
    async def _default_play(text: str, provider: str, playback_token: int) -> None:
        from core.services.tts.router import TTSRouter

        await TTSRouter.play(
            text,
            configured_provider=provider,
            tts_speaker=provider,
            playback_token=playback_token,
            log_prefix=f"TTS focus {provider}",
        )

    @staticmethod
    def _default_stop(playback_token: int) -> bool:
        import open_xiaoai_server

        stop = getattr(open_xiaoai_server, "stop_tts_playback", None)
        if stop is None:
            return False
        stop(playback_token)
        return True

    async def speak(self, text: str, *, token: FocusToken) -> SpeakResult:
        playback_token = next(_token_seq)
        self._tokens[token.token] = playback_token
        try:
            await self._play(text, self.name, playback_token)
        except Exception as exc:  # noqa: BLE001 - 单 provider 失败独立上报
            self._tokens.pop(token.token, None)
            return SpeakResult(self.name, "failed", False, reason=f"{type(exc).__name__}: {exc}"[:160])
        return SpeakResult(self.name, "ok", True, quality="strong",
                           reason="router_play_returned")

    async def cancel(self, token: FocusToken) -> bool:
        playback_token = self._tokens.pop(token.token, None)
        if playback_token is None:
            return False
        return bool(self._stop(playback_token))


class XiaoaiScriptSpeaker:
    """原生小爱 / 脚本回退：announce 阻塞播报；取消 = 停止设备音频。"""

    name = "xiaoai"

    def __init__(
        self,
        *,
        announce: Callable[[str], Awaitable[bool]] | None = None,
        stop: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._announce = announce or self._default_announce
        self._stop = stop or self._default_stop

    @staticmethod
    async def _default_announce(text: str) -> bool:
        from core.ref import get_speaker

        speaker = get_speaker()
        if speaker is None:
            return False
        async with speaker.announce(text=text, reason="tts focus", wait=True):
            return True

    @staticmethod
    async def _default_stop() -> bool:
        from core.ref import get_speaker

        speaker = get_speaker()
        if speaker is None:
            return False
        await speaker.stop_device_audio()
        return True

    async def speak(self, text: str, *, token: FocusToken) -> SpeakResult:
        try:
            ok = await self._announce(text)
        except Exception as exc:  # noqa: BLE001
            return SpeakResult(self.name, "failed", False, reason=f"{type(exc).__name__}: {exc}"[:160])
        if not ok:
            return SpeakResult(self.name, "failed", False, reason="announce_unavailable")
        return SpeakResult(self.name, "ok", True, quality="strong", reason="announce_returned")

    async def cancel(self, token: FocusToken) -> bool:
        return bool(await self._stop())
