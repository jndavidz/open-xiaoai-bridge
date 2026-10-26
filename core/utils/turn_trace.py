"""每轮对话的延迟埋点（TurnTrace，默认关闭）。

设计约束（ticket 11；P2 ticket 03 扩展用量记账面）：

- **开关分两层**：`turn_trace.enabled`（config.py，默认 false）或环境变量
  `TURN_TRACE_ENABLED`（1/true/yes/on 开，0/false/no/off 关）控制
  `[TurnTrace]` 日志行；`usage_ledger.enabled`（P2 票 03）单独控制用量台账
  落盘。**任一开启即建立轮对象**——台账开而日志关时只落盘不打日志；
  两者皆关时所有入口只做一次开关判断后返回，不产生日志、不改变任何行为。
- **只记数值**：每轮结束时输出**一条** `[TurnTrace]` INFO 行，内容是各段耗时
  （ms）、文案/回复长度（字符数）、工具轮数与 timeout/fallback 标志。
  **绝不输出 ASR 原文或回复文本**（PII）；backend/model/session_key 三个
  聚合维度只进 summary 字典喂 usage_ledger，不进日志行。
- **单轮锚点**：原生 ASR 链路以 `is_final` 到达为 t0；本地 ASR 链路没有这个
  事件，则在 LLM 请求开始时惰性建轮（ASR 段记为缺失）。
- **失败即静默**：埋点代码的任何异常都不得影响对话、播报或播放等待。

字段口径（`fmt` 中 -1 表示该段未观测到）：

    speech_end_to_asr_ms  本地链路「VAD 判定说完 → ASR 返回文本」（ticket 02；
                          原生 ASR 链路无说完信号，恒 -1）
    asr_to_llm_ms   ASR final → LLM 请求开始
    llm_first_token_ms  LLM 请求开始 → 首个非空文本 delta（voice-latency T06：
                        流式路径的 L1 桥接侧口径；非流式/无增量恒 -1）
    llm_ms          LLM 请求开始 → 完成（无流式，等价于全量耗时）
    llm_to_tts_ms   LLM 完成 → TTS 合成开始
    tts_ms          TTS 合成开始 → 结束（设备+云端）
    tts_to_play_ms  TTS 合成结束 → play_url 被接受
    accept_to_start_ms  play_url 接受 → 设备播放 started
    playback_ms     播放 started → finished
    total_ms        t0 → 最后一个观测点

轮次号同源（ticket 02）：对话循环经 core.utils.pipeline 分配轮次号后，
begin_turn(turn_id=...) 复用同一编号——`[TurnTrace] turn=N` 与 `[Pipeline]
turn=N` 日志行可按轮对齐；不传 turn_id 时维持内部自增（测试/非对话入口）。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from core.utils import usage_ledger
from core.utils.logger import logger

ENV_TURN_TRACE_ENABLED = "TURN_TRACE_ENABLED"
LOG_PREFIX = "[TurnTrace]"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# 输出字段顺序固定，便于日志解析与单测断言。
# 全数值：backend / model / session_key 三个聚合维度只进 summary 字典
# （喂 usage_ledger），不进日志行（任意字符串，且与 PII 纪律一致）。
_FIELDS = (
    "turn",
    "src",
    "incomplete",
    "asr_chars",
    "llm_chars",
    "tts_chars",
    "tool_rounds",
    "llm_requests",
    "prompt_chars",
    "web_calls",
    "prompt_tokens",
    "completion_tokens",
    "llm_error",
    "llm_timeout",
    "tts_ok",
    "tts_fallback",
    "play_url_ok",
    "playback_started",
    "playback_finished",
    "playback_timeout",
    "speech_end_to_asr_ms",
    "asr_to_llm_ms",
    "llm_first_token_ms",
    "llm_ms",
    "llm_to_tts_ms",
    "tts_ms",
    "tts_to_play_ms",
    "accept_to_start_ms",
    "playback_ms",
    "total_ms",
)

_MISSING_MS = -1.0


def _flag(raw: Any) -> bool | None:
    """把 1/true/yes/on、0/false/no/off 解析为 bool；其它（含空）返回 None。"""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None


def _read_config_enabled() -> bool:
    """读取 config.py 的 `turn_trace.enabled`（不构造 ConfigManager，避免联网）。"""
    try:
        from core.utils.config_loader import ensure_config_module_loaded

        module = ensure_config_module_loaded()
        app_config = getattr(module, "APP_CONFIG", None)
        if not isinstance(app_config, dict):
            return False
        section = app_config.get("turn_trace")
        if not isinstance(section, dict):
            return False
        return _flag(section.get("enabled")) is True
    except Exception:  # noqa: BLE001 - 配置不可读按关闭处理
        return False


def trace_enabled(config_reader: Callable[[], Any] | None = None) -> bool:
    """Trace 总开关：env 覆盖 config，默认 false。非法 env 值忽略。"""
    override = _flag(os.environ.get(ENV_TURN_TRACE_ENABLED))
    if override is not None:
        return override
    reader = config_reader or _read_config_enabled
    try:
        return bool(reader())
    except Exception:  # noqa: BLE001 - 读配置失败按关闭处理
        return False


def _int_or_missing(value: Any) -> int:
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return max(0, value)
    return -1


def _delta_ms(start: float | None, end: float | None) -> float:
    if start is None or end is None:
        return _MISSING_MS
    return round(max(0.0, (end - start) * 1000.0), 1)


def _tri_state(value: bool | None) -> int:
    if value is None:
        return -1
    return 1 if value else 0


class TurnTrace:
    """一轮对话的数值时间线（线程安全；不保存任何文本）。"""

    def __init__(
        self,
        turn_id: int,
        source: str = "",
        asr_chars: int | None = None,
        started_at: float | None = None,
    ) -> None:
        self.turn_id = int(turn_id)
        self.source = str(source or "")
        self.asr_chars = _int_or_missing(asr_chars) if asr_chars is not None else -1
        self.started_at = time.monotonic() if started_at is None else float(started_at)
        self._marks: dict[str, float] = {}
        self._lock = threading.RLock()

        self.llm_chars = -1
        self.tts_chars = -1
        self.tool_rounds = 0
        # 用量记账（P2 ticket 03）：请求/字符/联网/真实 token 累计。
        self.backend = ""
        self.model = ""
        self.session_key = ""
        self.prompt_chars = -1
        self.llm_requests = 0
        self.web_calls = 0
        self.prompt_tokens = -1
        self.completion_tokens = -1
        self.llm_error = False
        self.llm_timeout = False
        self.tts_synth_ok: bool | None = None
        self.tts_fallback = False
        self.play_url_accepted: bool | None = None
        self.playback_started = False
        self.playback_finished: bool | None = None
        self.playback_timeout = False

        if self.source == "xiaoai_asr":
            # ASR final 到达即 t0，避免下游各段再各自取 now。
            self._marks["asr_final"] = self.started_at

    # ---- 时间线 ----

    @property
    def marks(self) -> dict[str, float]:
        with self._lock:
            return dict(self._marks)

    def has_phase(self, phase: str) -> bool:
        with self._lock:
            return phase in self._marks

    def mark(self, phase: str, at: float | None = None) -> float:
        """记录某阶段的首次发生时间（重复调用保留第一次）。"""
        with self._lock:
            if phase in self._marks:
                return self._marks[phase]
            when = time.monotonic() if at is None else float(at)
            self._marks[phase] = when
            return when

    # ---- 计数与标志（只接受长度/布尔，不接受文本）----

    def note_reply_chars(self, chars: int) -> None:
        with self._lock:
            self.llm_chars = _int_or_missing(chars)

    def note_tts_chars(self, chars: int) -> None:
        """累计送入 TTS 的字符数（P1 ticket 04 起逐段累加：分段播放时
        每段各记一次，单段调用与覆盖口径等价；非法值忽略）。"""
        with self._lock:
            if isinstance(chars, bool) or not isinstance(chars, (int, float)):
                return
            if chars < 0:
                return
            value = int(chars)
            self.tts_chars = value if self.tts_chars < 0 else self.tts_chars + value

    def note_tool_rounds(self, rounds: int) -> None:
        with self._lock:
            self.tool_rounds = max(self.tool_rounds, _int_or_missing(rounds))

    def note_context(self, backend: str, model: str, session_key: str) -> None:
        """聚合维度一次注入（首写优先：一轮只属于一个后端请求路径）。"""
        with self._lock:
            if not self.backend:
                self.backend = str(backend or "")
            if not self.model:
                self.model = str(model or "")
            if not self.session_key:
                self.session_key = str(session_key or "")

    def note_prompt_chars(self, chars: int) -> None:
        """累计本次请求发送的 prompt 字符数（工具回环每轮重发历史，逐次累加）。"""
        with self._lock:
            if isinstance(chars, bool) or not isinstance(chars, (int, float)):
                return
            if chars < 0:
                return
            value = int(chars)
            self.prompt_chars = value if self.prompt_chars < 0 else self.prompt_chars + value

    def note_llm_request(self) -> None:
        with self._lock:
            self.llm_requests += 1

    def note_web_calls(self, count: int = 1) -> None:
        """联网工具调用数（出公网的工具，如天气；LAN 工具不计）。"""
        with self._lock:
            if count > 0:
                self.web_calls += int(count)

    def note_usage_tokens(self, prompt: Any = None, completion: Any = None) -> None:
        """累计上游自报的真实 token（缺失/非法值忽略，保持 -1 = 未观测）。"""
        with self._lock:
            for attr, value in (
                ("prompt_tokens", prompt),
                ("completion_tokens", completion),
            ):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if value < 0:
                    continue
                current = getattr(self, attr)
                setattr(self, attr, int(value) if current < 0 else current + int(value))

    def note_llm_error(self) -> None:
        with self._lock:
            self.llm_error = True

    def note_llm_timeout(self) -> None:
        with self._lock:
            self.llm_timeout = True

    def note_tts_result(self, ok: bool, fallback: bool = False) -> None:
        """TTS 结果首置优先：provider 错误（ok=False/fallback=True）不被
        fallback 播放的成功覆写——「TTS provider 错误记录」缺口的关键语义
        （ticket 02；生产实测 turn19 类失败曾因此在埋点里不可见）。"""
        with self._lock:
            if self.tts_synth_ok is not None:
                return
            self.tts_synth_ok = bool(ok)
            self.tts_fallback = bool(fallback)

    def note_play_url(self, accepted: bool) -> None:
        with self._lock:
            self.play_url_accepted = bool(accepted)

    def note_playback(
        self,
        *,
        finished: bool,
        started: bool,
        reason: str,
        wait_started_at: float | None,
        started_at_ms: float | None,
        finished_at_ms: float | None,
        now: float | None = None,
    ) -> None:
        """播放等待收尾：把适配器的相对时间线换算到本轮的绝对时间线。"""
        wait_start = float(wait_started_at) if wait_started_at is not None else None
        with self._lock:
            if started and wait_start is not None and started_at_ms is not None:
                self.mark(
                    "playback_start",
                    wait_start + max(0.0, float(started_at_ms)) / 1000.0,
                )
            if finished:
                if wait_start is not None and finished_at_ms is not None:
                    end = wait_start + max(0.0, float(finished_at_ms)) / 1000.0
                else:
                    end = time.monotonic() if now is None else float(now)
                self.mark("playback_end", end)
            self.playback_started = bool(started)
            self.playback_finished = bool(finished)
            self.playback_timeout = str(reason) == "timeout"

    # ---- 汇总 ----

    def summary(self, incomplete: bool = False) -> dict[str, Any]:
        """返回全数值字段（缺失段为 -1）。"""
        with self._lock:
            marks = dict(self._marks)
            asr_chars = self.asr_chars
            llm_chars = self.llm_chars
            tts_chars = self.tts_chars
            tool_rounds = self.tool_rounds
            backend = self.backend
            model = self.model
            session_key = self.session_key
            prompt_chars = self.prompt_chars
            llm_requests = self.llm_requests
            web_calls = self.web_calls
            prompt_tokens = self.prompt_tokens
            completion_tokens = self.completion_tokens
            llm_error = self.llm_error
            llm_timeout = self.llm_timeout
            tts_ok = self.tts_synth_ok
            tts_fallback = self.tts_fallback
            play_url = self.play_url_accepted
            playback_started = self.playback_started
            playback_finished = self.playback_finished
            playback_timeout = self.playback_timeout

        asr = marks.get("asr_final")
        speech_end = marks.get("speech_end")
        llm_start = marks.get("llm_start")
        llm_first_token = marks.get("llm_first_token")
        llm_end = marks.get("llm_end")
        tts_start = marks.get("tts_start")
        tts_end = marks.get("tts_end")
        play_accepted = marks.get("play_url")
        play_start = marks.get("playback_start")
        play_end = marks.get("playback_end")

        anchor = asr if asr is not None else llm_start
        if anchor is None:
            anchor = self.started_at
        last = max([anchor, *marks.values()])

        return {
            "turn": self.turn_id,
            "src": self.source or "unknown",
            "incomplete": 1 if incomplete else 0,
            "asr_chars": asr_chars,
            "llm_chars": llm_chars,
            "tts_chars": tts_chars,
            "tool_rounds": tool_rounds,
            "backend": backend,
            "model": model,
            "session_key": session_key,
            "prompt_chars": prompt_chars,
            "llm_requests": llm_requests,
            "web_calls": web_calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "llm_error": 1 if llm_error else 0,
            "llm_timeout": 1 if llm_timeout else 0,
            "tts_ok": _tri_state(tts_ok),
            "tts_fallback": 1 if tts_fallback else 0,
            "play_url_ok": _tri_state(play_url),
            "playback_started": 1 if playback_started else 0,
            "playback_finished": _tri_state(playback_finished),
            "playback_timeout": 1 if playback_timeout else 0,
            "speech_end_to_asr_ms": _delta_ms(speech_end, asr),
            "asr_to_llm_ms": _delta_ms(asr, llm_start) if asr is not None else _MISSING_MS,
            "llm_first_token_ms": _delta_ms(llm_start, llm_first_token),
            "llm_ms": _delta_ms(llm_start, llm_end),
            "llm_to_tts_ms": _delta_ms(llm_end, tts_start),
            "tts_ms": _delta_ms(tts_start, tts_end),
            "tts_to_play_ms": _delta_ms(tts_end, play_accepted),
            "accept_to_start_ms": _delta_ms(play_accepted, play_start),
            "playback_ms": _delta_ms(play_start, play_end),
            "total_ms": _delta_ms(anchor, last),
        }


def format_turn_trace(summary: dict[str, Any]) -> str:
    """把汇总字典格式化成单行 `[TurnTrace] key=value ...`（全数值）。"""
    parts = [LOG_PREFIX]
    for key in _FIELDS:
        value = summary.get(key)
        if key == "turn":
            parts.append(f"turn={int(value or 0)}")
        elif key == "src":
            parts.append(f"src={value}")
        elif key.endswith("_ms"):
            try:
                parts.append(f"{key}={float(value):.1f}")
            except (TypeError, ValueError):
                parts.append(f"{key}={_MISSING_MS:.1f}")
        else:
            try:
                parts.append(f"{key}={int(value)}")
            except (TypeError, ValueError):
                parts.append(f"{key}=-1")
    return " ".join(parts)


# ---- 全局「当前轮」（bridge 单设备、轮次串行）----

_LOCK = threading.RLock()
_current: TurnTrace | None = None
_turn_counter = 0


def current_trace() -> TurnTrace | None:
    with _LOCK:
        return _current


def reset_turn_trace(reset_counter: bool = False) -> None:
    """测试辅助：丢弃当前轮（不发日志）。

    reset_counter=True 时连轮次计数器一并清零——供传入了显式 turn_id 的
    测试恢复进程级干净状态（生产路径永不调用本函数）。
    """
    global _current, _turn_counter
    with _LOCK:
        _current = None
        if reset_counter:
            _turn_counter = 0


def abandon_turn() -> None:
    """当前轮已确定不会继续（如命中退出词）：按 incomplete 输出并清空。"""
    global _current
    try:
        with _LOCK:
            trace = _current
            if trace is None:
                return
            _current = None
        _emit(trace, incomplete=True)
    except Exception:  # noqa: BLE001 - 埋点不得影响主流程
        pass


def _new_trace_locked(
    source: str,
    asr_chars: int | None = None,
    turn_id: int | None = None,
) -> TurnTrace:
    global _current, _turn_counter
    if turn_id is None:
        _turn_counter += 1
        turn_id = _turn_counter
    else:
        # 传入号（pipeline 源）同样推进内部计数器——后续自增号不会与
        # 传入号重叠，两号源不产生相同的 turn=N。
        _turn_counter = max(_turn_counter, turn_id)
    _current = TurnTrace(turn_id, source=source, asr_chars=asr_chars)
    return _current


def _emit(trace: TurnTrace, incomplete: bool = False) -> None:
    try:
        summary = trace.summary(incomplete=incomplete)
        # 日志行开关独立于台账：trace 关、台账开时只落盘不打日志。
        if trace_enabled():
            logger.info(format_turn_trace(summary))
        if usage_ledger.enabled():
            usage_ledger.observe(summary)
    except Exception:  # noqa: BLE001 - 埋点不得影响主流程
        pass


def _recording() -> bool:
    """任一记账面开启（[TurnTrace] 日志行或用量台账）都需要轮对象存在。"""
    return trace_enabled() or usage_ledger.enabled()


def begin_turn(
    source: str = "xiaoai_asr",
    asr_chars: int | None = None,
    turn_id: int | None = None,
) -> TurnTrace | None:
    """开始新一轮；若上一轮未收尾，先按 incomplete 输出（每轮仍只有一条）。

    turn_id：对话循环传入 core.utils.pipeline 的轮次号，使埋点行与管线
    日志同号；缺省时内部自增（测试/非对话入口的兼容口径）。
    """
    global _current
    try:
        if not _recording():
            return None
        with _LOCK:
            previous = _current
            trace = _new_trace_locked(source, asr_chars, turn_id=turn_id)
        if previous is not None:
            _emit(previous, incomplete=True)
        return trace
    except Exception:  # noqa: BLE001
        return None


def _update(action: Callable[[TurnTrace], None], *, ensure_source: str | None = None) -> None:
    try:
        with _LOCK:
            trace = _current
            if trace is None:
                if ensure_source is None or not _recording():
                    return
                trace = _new_trace_locked(ensure_source)
            action(trace)
    except Exception:  # noqa: BLE001 - 埋点不得影响主流程
        pass


def mark_llm_start() -> None:
    _update(lambda t: t.mark("llm_start"), ensure_source="llm")


def mark_llm_first_token() -> None:
    """首个非空文本 delta 到达（voice-latency T06）：重复调用保留第一次。"""
    _update(lambda t: t.mark("llm_first_token"))


def mark_llm_end() -> None:
    _update(lambda t: t.mark("llm_end"))


def note_reply_chars(chars: int) -> None:
    _update(lambda t: t.note_reply_chars(chars))


def note_tool_rounds(rounds: int) -> None:
    _update(lambda t: t.note_tool_rounds(rounds))


def note_context(backend: str, model: str, session_key: str) -> None:
    """注入聚合维度（backend/model/session_key，首写优先）。"""
    _update(lambda t: t.note_context(backend, model, session_key))


def note_prompt_chars(chars: int) -> None:
    _update(lambda t: t.note_prompt_chars(chars))


def note_llm_request() -> None:
    _update(lambda t: t.note_llm_request())


def note_web_calls(count: int = 1) -> None:
    _update(lambda t: t.note_web_calls(count))


def note_usage_tokens(prompt: Any = None, completion: Any = None) -> None:
    _update(lambda t: t.note_usage_tokens(prompt, completion))


def note_llm_error() -> None:
    _update(lambda t: t.note_llm_error())


def note_llm_timeout() -> None:
    _update(lambda t: t.note_llm_timeout())


def mark_tts_start() -> None:
    _update(lambda t: t.mark("tts_start"))


def mark_tts_end() -> None:
    _update(lambda t: t.mark("tts_end"))


def note_tts_chars(chars: int) -> None:
    _update(lambda t: t.note_tts_chars(chars))


def note_tts_result(ok: bool, fallback: bool = False) -> None:
    _update(lambda t: t.note_tts_result(ok, fallback))


def note_play_url(accepted: bool) -> None:
    _update(lambda t: t.note_play_url(accepted))


def mark_play_url() -> None:
    """播放起点链（ticket 02）：play_url 调用返回的时刻戳（无论接受与否——
    拒绝轮的 tts_to_play_ms 仍反映合成→请求的耗时）——补齐
    tts_to_play_ms / accept_to_start_ms 两段（此前只有布尔标志、无时间戳，
    生产实测这两段全 -1）。"""
    _update(lambda t: t.mark("play_url"))


def note_playback(
    *,
    finished: bool,
    started: bool,
    reason: str,
    wait_started_at: float | None,
    started_at_ms: float | None,
    finished_at_ms: float | None,
) -> None:
    """播放适配器收尾：仅收尾「已经进入 TTS 合成」的轮，避免外部播报误收尾。"""
    global _current
    try:
        with _LOCK:
            trace = _current
            if trace is None or not trace.has_phase("tts_start"):
                return
            trace.note_playback(
                finished=finished,
                started=started,
                reason=reason,
                wait_started_at=wait_started_at,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
            )
            _current = None
        _emit(trace)
    except Exception:  # noqa: BLE001 - 埋点不得影响播放判定
        pass
