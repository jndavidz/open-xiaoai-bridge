"""对话循环发声接缝（B-29 步 2，评审 S4 design-it-twice 方案 A）。

不变量：**对话循环内的发声必须发生在物理断录之后**——旧实现靠
`_begin_speaking`（状态迁移 + 断录）与调用次序纪律保证，不变量只活在注释里。
本模块把不变量做成结构：播放 API（`play_pcm` / `play_segment_tts`）只存在于
`open()` 产出的 `SpeakingSession` 上，而 `open()` 进门即物理断录——不经
open() 无从发声，新调用点想错都错不了。

边界（刻意不吸进 announce 的职责，两套防护对应两种威胁）：
- mic_gate（固件闭麦）防固件唤醒误触，**挡不住 bridge 自身 arecord 喂给
  VAD/KWS 的回声**——本模块的物理断录才是对话回声防护的真值；
- KWS 闸门与词表检查属 `speaker.announce`（一次性对外播报），对话内流式
  句段不经过它（词表检查会误杀含唤醒词的合法 LLM 回复）。

完成等待维持 `playback_timing.sleep_playback_done` 算术原语：Rust PCM 通道
没有完成事件（通道返回即完成，buffer 播放连返回都不等），事件面观测不到。
"""

from contextlib import asynccontextmanager

from core.ref import get_speaker
from core.services.audio.playback_timing import sleep_playback_done
from core.utils.logger import logger


class SpeakingChannelError(RuntimeError):
    """SpeakingChannel 非法使用（如前一个 session 未退出又 open）。"""


class SpeakingSession:
    """一次断录窗口内的发声句柄：播放 API 只在此暴露。"""

    def __init__(self, channel: "SpeakingChannel"):
        self._channel = channel
        self.outcome: dict | None = None  # 预留：完成等待结局的可观测面

    @property
    def _controller(self):
        return self._channel._controller

    async def play_pcm(self, pcm: bytes, *, reason: str = "pcm") -> None:
        """播放 PCM 缓冲（提示音），并按算术原语等完成。

        空 buffer / 无 speaker 静默跳过；失败只记日志不抛——对话不能死在
        提示音上（与旧 _play_notify/_play_send_sound 同口径）。
        """
        if not pcm:
            return
        speaker = get_speaker()
        if not speaker:
            return
        try:
            await speaker.play(buffer=pcm)
            await sleep_playback_done(pcm)
        except Exception as exc:
            logger.debug(f"{reason} sound error: {exc}", module=self._controller.LOG_MODULE)

    async def play_segment_tts(self, segment: str) -> None:
        """播放单个回复句段（令牌与检查点由调用方的段循环管理）。

        TTS 失败降级 speaker.play(text=...)（既有口径）；TurnCancelledError
        原样上抛——取消传播语义不在此吞。
        """
        from core.utils.pipeline import TurnCancelledError

        try:
            await self._controller.backend._play_response_with_tts(
                segment,
                tts_speaker=self._controller.backend.get_tts_speaker_for_session_key(),
                playback_token=self._controller._playback_token,
            )
        except TurnCancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"TTS playback error: {exc}",
                module=self._controller.LOG_MODULE,
            )
            # fallback 文本播报经控制器旁路闸门（B-29 步 3：默认裸 play，
            # conversation.bypass_play_via_announce=true 时走 announce）
            await self._controller._play_bypass_text(segment, reason="tts fallback")


class SpeakingChannel:
    """控制器持有的发声通道：open() 进门断录，播放 API 只在 session 上。"""

    def __init__(self, controller):
        self._controller = controller
        self._active: SpeakingSession | None = None

    @asynccontextmanager
    async def open(self, *, reason: str = "speak", stop_recording: bool = True):
        """进入一次断录发声窗口。

        stop_recording=False：复用调用方已完成的断录窗口（如句段播完后的
        notify——再断录是一次冗余 RPC，且在轮间隙关键路径上）。
        重入拒绝：同一控制器的窗口必须串行，嵌套即误用。
        """
        if self._active is not None:
            raise SpeakingChannelError(
                f"speaking session already open ({reason}); sessions must not nest"
            )
        if stop_recording:
            await self._controller._stop_recording()
        session = SpeakingSession(self)
        self._active = session
        try:
            yield session
        finally:
            self._active = None
