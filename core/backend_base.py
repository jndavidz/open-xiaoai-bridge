"""外部后端管理器公共骨架（BackendManagerBase，C1 第一步）。

把三个外部后端管理器（OpenAI / QwenPaw / OpenClaw）重复的公共骨架上提为
单一基类；子类只实现各自的传输核：

    - 状态类属性：启用位、会话键、规则提示 ×2、TTS provider/音色/语速、
      每会话音色表、响应等待超时；
    - 配置装载与热重载监听（模板方法：reload_from_config → _apply_common_config
      + 子类 _apply_config 钩子）；
    - run-id 与 Future 记账（_open_run / _signal_response_ready / close）；
    - send 与 send-and-play 双入口（send / send_and_play_reply）；
    - 等待与超时（_wait_response）；
    - TTS 播放委托（统一经 TTSRouter.play）；
    - 延迟埋点挂点（TurnTrace：mark_llm_start / note_reply_chars /
      note_llm_error / note_llm_timeout / note_tool_rounds / mark_llm_end）。

子类契约（classmethod 单例风格，与既有管理器一致）：

    - CONFIG_PREFIX / ENABLE_ENV_VARS / LOG_MODULE 三个身份属性；
    - _apply_config(config)：装载子类专属配置（传输核参数）；
    - _request_completion(text, stats=None) -> str | None：传输核——一次完整
      的「发请求 → 拿回复」；stats 是 {"tool_rounds": int} 记账字典，供
      工具回环类传输核累加轮数。

本类不直接实例化、不直接使用；所有方法均为 classmethod。
"""

import asyncio
import uuid

from core.utils.base import get_env
from core.utils.config import ConfigManager
from core.utils import turn_trace
from core.utils.logger import logger

_TRUE_VALUES = ("1", "true", "yes")


class BackendManagerBase:
    """外部后端管理器基类：公共骨架在此，传输核在子类。"""

    # —— 子类身份（必填） ——
    # APP_CONFIG 配置前缀，例如 "openai"
    CONFIG_PREFIX: str = ""
    # 启用环境变量名（按序探测，先命中先算）；例如 ("OPENAI_ENABLE",)
    ENABLE_ENV_VARS: tuple[str, ...] = ()
    # 日志模块名，例如 "OpenAI"；日志行形如 "[OpenAI] ..."
    LOG_MODULE: str = ""
    # 会话键配置缺省值（config 未配 session_key 时的回落）
    DEFAULT_SESSION_KEY: str = ""

    XIAOAI_TTS_SPEAKER = "xiaoai"

    _initialized = False
    _reload_listener_registered = False
    _enabled = False
    _session_key = ""
    _rule_prompt = ""
    _rule_prompt_for_skill = ""
    _tts_provider: str | None = None
    _tts_speaker = None
    _session_tts_speakers: dict[str, str] = {}
    _tts_speed = 1.0
    # 响应等待超时（秒）；config key 统一为 response_timeout
    _response_timeout = 120
    _response_events: dict[str, asyncio.Future] = {}
    _response_texts: dict[str, str] = {}
    _response_tts_speakers: dict[str, str | None] = {}
    last_error: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 记账表是类级单例状态：给每个子类独立副本，避免多个后端
        # 经基类属性查找共享同一份 dict。
        for name in (
            "_response_events",
            "_response_texts",
            "_response_tts_speakers",
            "_session_tts_speakers",
        ):
            setattr(cls, name, {})

    # —— 配置装载与热重载 -------------------------------------------------

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        logger.info(f"[{cls.LOG_MODULE}] Initializing from config...")
        cls.reload_from_config(enabled=enabled)
        cls._initialized = True

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        """从配置中心刷新本后端配置（注册热重载监听，仅一次）。"""
        config_manager = ConfigManager.instance()
        if not cls._reload_listener_registered:
            config_manager.add_reload_listener(
                lambda _old, _new: cls.reload_from_config()
            )
            cls._reload_listener_registered = True

        config = config_manager.get_app_config(cls.CONFIG_PREFIX, {})

        if enabled is not None:
            cls._enabled = enabled
        else:
            cls._enabled = cls._enabled_from_env()

        cls._apply_common_config(config)
        cls._apply_config(config)
        if cls._enabled:
            cls._log_enabled()

    @classmethod
    def _enabled_from_env(cls) -> bool:
        """参数缺省时的启用判定：环境变量按序探测，未设为 False。"""
        for name in cls.ENABLE_ENV_VARS:
            raw = get_env(name)
            if raw is not None:
                return raw.lower() in _TRUE_VALUES
        return False

    @classmethod
    def _apply_common_config(cls, config: dict):
        """装载所有后端共有的配置段（TTS / 规则提示 / 等待超时）。"""
        cls._response_timeout = int(config.get("response_timeout", 120))
        configured_provider = config.get("tts_provider")
        cls._tts_provider = (
            str(configured_provider).strip().lower()
            if configured_provider
            else None
        )
        cls._tts_speaker = config.get("tts_speaker", None)
        raw_session_speakers = config.get("session_tts_speakers", {})
        cls._session_tts_speakers = (
            {
                str(key): str(value)
                for key, value in raw_session_speakers.items()
                if key and value
            }
            if isinstance(raw_session_speakers, dict)
            else {}
        )
        cls._tts_speed = float(config.get("tts_speed", 1.0))
        cls._rule_prompt = str(config.get("rule_prompt", "") or "")
        cls._rule_prompt_for_skill = str(config.get("rule_prompt_for_skill", "") or "")

    @classmethod
    def _apply_config(cls, config: dict):
        """子类钩子：装载传输核专属配置（默认无）。"""

    @classmethod
    def _log_enabled(cls):
        """子类钩子：启用后的日志行（默认无）。"""

    @classmethod
    def _optional_float(cls, value) -> float | None:
        if value is None or value == "":
            return None
        return float(value)

    @classmethod
    def _optional_int(cls, value) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    # —— 生命周期与状态 ---------------------------------------------------

    @classmethod
    async def connect(cls) -> bool:
        """空连接钩子，保持与 WebSocket 后端的接口对齐。"""
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._enabled

    @classmethod
    async def close(cls):
        """取消所有未完成的响应等待者。"""
        for waiter in list(cls._response_events.values()):
            if not waiter.done():
                waiter.cancel()
        cls._response_events.clear()
        cls._response_texts.clear()
        cls._response_tts_speakers.clear()

    @classmethod
    def is_connected(cls) -> bool:
        return cls.is_enabled()

    @classmethod
    def is_enabled(cls) -> bool:
        if not cls._initialized:
            cls.initialize_from_config()
        return cls._enabled

    @classmethod
    def set_session_key(cls, session_key: str):
        logger.info(
            f"[{cls.LOG_MODULE}] Session key updated: "
            f"{cls._session_key!r} -> {session_key!r}"
        )
        cls._session_key = session_key

    @classmethod
    def reset_session_key(cls, config_manager=None):
        """把会话键复位到当前配置默认值（每次唤醒前调用；静默，不打日志）。

        这是会话键复位的唯一公开接口——消费方不得直写 _session_key。
        与 config.get("session_key", DEFAULT_SESSION_KEY) 同语义：键缺失
        回落默认，显式空串原样保留（OpenAI 用空串禁用会话头，不得吞掉）。
        """
        if config_manager is None:
            config_manager = ConfigManager.instance()
        cls._session_key = config_manager.get_app_config(
            cls.CONFIG_PREFIX, {}
        ).get("session_key", cls.DEFAULT_SESSION_KEY)

    @classmethod
    def rule_prompt_for(cls, and_play: bool) -> str:
        """取规则提示：播放路径用 rule_prompt，纯发送路径用 rule_prompt_for_skill。"""
        return cls._rule_prompt if and_play else cls._rule_prompt_for_skill

    @classmethod
    def get_tts_speaker_for_session_key(cls, session_key: str | None = None) -> str | None:
        target_session_key = session_key or cls._session_key
        return cls._session_tts_speakers.get(target_session_key) or cls._tts_speaker

    # —— run-id 与 Future 记账 --------------------------------------------

    @classmethod
    def _open_run(cls, run_id: str, tts_speaker: str | None):
        """为一次 run 建立记账三元组（Future / 文本槽 / 音色槽）。"""
        loop = asyncio.get_running_loop()
        cls._response_events[run_id] = loop.create_future()
        cls._response_texts[run_id] = ""
        cls._response_tts_speakers[run_id] = tts_speaker

    @classmethod
    def _signal_response_ready(cls, run_id: str):
        """以线程安全方式唤醒响应等待者。"""
        waiter = cls._response_events.get(run_id)
        if not waiter or waiter.done():
            return
        cls._on_response_finalized(run_id)
        fut_loop = waiter.get_loop()
        fut_loop.call_soon_threadsafe(waiter.set_result, None)

    @classmethod
    def _on_response_finalized(cls, run_id: str):
        """钩子：响应定稿即将唤醒等待者（守卫内，单次）；子类可在此记埋点。"""

    # —— send 与 send-and-play 双入口 --------------------------------------

    @classmethod
    async def send(cls, text: str, wait_response: bool = False) -> str | None:
        run_id = await cls._send_and_track(text)
        if run_id is None:
            return None
        if not wait_response:
            asyncio.create_task(cls._discard_response(run_id))
            return run_id
        try:
            return await cls._wait_response(run_id)
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    @classmethod
    async def _discard_response(cls, run_id: str):
        """fire-and-forget：等传输核自然收尾并清光记账（含音色槽）。"""
        try:
            await cls._wait_response(run_id)
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    @classmethod
    async def send_and_play_reply(cls, text: str, wait_response: bool = False) -> str | None:
        run_id = await cls._send_and_track(text)
        if run_id is None:
            return None
        if not wait_response:
            asyncio.create_task(cls._wait_and_play_response(run_id))
            return run_id
        try:
            response_text = await cls._wait_response(run_id)
            if response_text:
                await cls._play_response_with_tts(
                    response_text,
                    tts_speaker=cls._response_tts_speakers.get(run_id),
                )
            return response_text
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    @classmethod
    async def _send_and_track(cls, text: str) -> str | None:
        """发送并记账：建 run、起传输核任务，返回 run_id（禁用返回 None）。"""
        if not cls._initialized:
            cls.initialize_from_config()
        if not cls._enabled:
            logger.warning(
                f"[{cls.LOG_MODULE}] send called but backend is disabled"
            )
            return None

        run_id = str(uuid.uuid4())
        cls._open_run(run_id, cls.get_tts_speaker_for_session_key())
        # TurnTrace（ticket 11）：本地 ASR 链路没有 is_final 事件，在这里惰性
        # 建轮；原生 ASR 链路此时已有当前轮，只补 LLM 起始时间。
        turn_trace.mark_llm_start()
        logger.user_speech(text, module=f"{cls.LOG_MODULE}({cls._session_key})")
        asyncio.create_task(cls._run_transport(run_id, text))
        return run_id

    # —— 等待与超时 --------------------------------------------------------

    @classmethod
    async def _wait_response(cls, run_id: str) -> str | None:
        event = cls._response_events.get(run_id)
        if not event:
            logger.warning(f"[{cls.LOG_MODULE}] No event found for run {run_id}")
            return None
        try:
            await asyncio.wait_for(event, timeout=cls._response_timeout)
            return cls._response_texts.pop(run_id, "") or None
        except asyncio.TimeoutError:
            turn_trace.note_llm_timeout()
            logger.warning(
                f"[{cls.LOG_MODULE}] Timeout waiting for response (runId: {run_id})"
            )
            return None
        finally:
            cls._response_events.pop(run_id, None)
            cls._response_texts.pop(run_id, None)

    @classmethod
    async def _wait_and_play_response(cls, run_id: str):
        try:
            response_text = await cls._wait_response(run_id)
            if response_text:
                await cls._play_response_with_tts(
                    response_text,
                    tts_speaker=cls._response_tts_speakers.get(run_id),
                )
            else:
                logger.warning(
                    f"[{cls.LOG_MODULE}] No response text received for run {run_id}"
                )
        finally:
            cls._response_tts_speakers.pop(run_id, None)

    # —— TTS 播放委托（统一 TTS 路由） --------------------------------------

    @classmethod
    async def _play_response_with_tts(
        cls,
        text: str,
        tts_speaker: str | None = None,
        playback_token: int | None = None,
    ):
        """合成文本并经音箱播放。"""
        from core.services.tts.router import TTSRouter

        resolved_tts_speaker = tts_speaker or cls.get_tts_speaker_for_session_key()
        await TTSRouter.play(
            text,
            configured_provider=cls._tts_provider,
            tts_speaker=resolved_tts_speaker,
            tts_speed=cls._tts_speed,
            playback_token=playback_token,
            log_prefix=cls.LOG_MODULE,
        )

    # —— 流式响应通道（P1 ticket 03） --------------------------------------

    @classmethod
    def stream_enabled(cls) -> bool:
        """流式开关（per-provider，默认关）。子类按配置与接口规格覆写。"""
        return False

    @classmethod
    async def stream_response_segments(cls, text: str):
        """流式响应通道：产出回复句段（async generator，P1 ticket 03）。

        默认实现 = 非流式退化「单句序列」：整段回复作为单个句段产出——
        非流式后端（openai_responses / anthropic_messages / OpenClaw /
        QwenPaw）与流式后端共用对话控制器的同一消费路径。
        支持流式的子类（OpenAI chat_completions + stream 开）覆写为真流式。
        """
        response = await cls.send(text, wait_response=True)
        if response:
            yield response

    # —— 传输核模板 --------------------------------------------------------

    @classmethod
    async def _run_transport(cls, run_id: str, text: str):
        """调传输核、落文本、记埋点、唤醒等待者。"""
        # TurnTrace（ticket 11）：只统计轮数与耗时，不记录任何文本。
        stats = {"tool_rounds": 0}
        try:
            response_text = await cls._request_completion(text, stats=stats)
            if response_text:
                cls._response_texts[run_id] = response_text
                turn_trace.note_reply_chars(len(response_text))
                logger.ai_response(
                    response_text, module=f"{cls.LOG_MODULE}({cls._session_key})"
                )
            else:
                turn_trace.note_llm_error()
        except Exception as exc:
            cls.last_error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, asyncio.TimeoutError):
                turn_trace.note_llm_timeout()
            turn_trace.note_llm_error()
            logger.error(
                f"[{cls.LOG_MODULE}] Request failed: {cls.last_error}"
            )
        finally:
            turn_trace.note_tool_rounds(stats["tool_rounds"])
            turn_trace.mark_llm_end()
            cls._signal_response_ready(run_id)

    @classmethod
    async def _request_completion(
        cls, text: str, stats: dict[str, int] | None = None
    ) -> str | None:
        """子类钩子：传输核——一次完整的「发请求 → 拿回复」。

        stats 是 {"tool_rounds": int} 记账字典，工具回环类传输核累加轮数；
        HTTP>=400 之类的可预期失败返回 None，意外异常向上抛由 _run_transport
        统一记账。
        """
        raise NotImplementedError("backend transport core must implement _request_completion")
