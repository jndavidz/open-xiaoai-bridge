"""播放完成事件适配器：把三种事件源收敛成「开始 / 结束 / 超时」。

对上层只暴露两件事：

    done = await adapter.wait_for_finish(timeout_ms, start_grace_ms)
    done.finished / done.started / done.reason / done.quality / done.paused
    adapter.snapshot()

三种事件源（`event_plane.source` 选主源，`shadow_sources` 并行比对）：

  - ``context``（默认）：每 ``poll_interval_ms`` 轮询能力层
    ``player_context().info.status``（1=播放中，0=空闲，2=暂停）。这是 ticket 03
    阻塞播报的原始行为，原样搬进本模块，作为统一入口。
  - ``client``：client 侧 ``playing`` 事件（``XiaoAI.on_event`` 的 playing
    分支喂入）。``playing`` → 开始；``idle`` → 结束；``paused`` 不算结束。
  - ``native``：设备原生 ``play_status_broadcast_event``（``{"status":1/0}``）。
    部署事实：运行时镜像没有 openssh-client，client 的 ``run_shell`` 也不能
    流式输出 ``ubus listen``。因此 native 源是「可插拔但默认关闭」：只有配置了
    ``event_plane.native_cmd``（外部命令，例如未来容器内可用的 ssh）才启动
    subprocess 拉流；不加 Docker 依赖、不改部署。

影子模式：主源决定 ``wait_for_finish`` 的返回值；影子源只并行记录开始/结束
时间线，差异写入 ``snapshot()`` 与结构化日志，绝不阻塞或改变等待结果。

硬边界（docs/devices/lx06/ubus-events-play-status.md §4）：

  - 事件不带 id ⇒ 本适配器只保证**串行播报**语义，不支持并发播报关联。
  - ``status:0`` 的语义是「播放链路空闲」，不是「播放成功」。
  - ``miplayer -f``（含阻塞 TTS 脚本兜底）不产生原生事件 ⇒ 必须保留超时兜底。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, NamedTuple

from core.utils.logger import logger
from core.utils import turn_trace

__all__ = [
    "Completion",
    "DEFAULT_EVENT_PLANE",
    "EventPlaneConfig",
    "PlaybackEventAdapter",
    "get_playback_events",
    "parse_native_play_status",
    "reset_playback_events",
    "set_playback_events",
]


class Completion(NamedTuple):
    """一次完成等待的结论。

    ``quality=strong`` ⇔ 主源亲自观察到起播**和**结束（context idle / native
    finished_event）；影子兜底、宽限兜底、超时、未见起播一律 ``weak``。
    ``paused=True`` 表示等待期间看到过暂停且之后没有恢复——暂停后的 idle 不能
    当作「播完」。
    """

    finished: bool
    started: bool
    reason: str
    quality: str | None = None   # strong | weak | None（未完成）
    source: str | None = None    # 完成事实来自哪个源（primary 或影子）
    paused: bool = False         # 等待期间出现过未恢复的暂停

    @property
    def is_strong(self) -> bool:
        return self.quality == "strong"

SOURCES: tuple[str, ...] = ("context", "client", "native")
DEFAULT_SOURCE = "context"
DEFAULT_SHADOW_SOURCES: tuple[str, ...] = ("client",)
DEFAULT_POLL_INTERVAL_MS = 200.0
DEFAULT_START_GRACE_MS = 1000.0

# `player_get_context.info.status`：1 = 播放中；0/2 = 链路空闲（0 空闲、2 暂停）。
CONTEXT_PLAYING_STATUS = 1
CONTEXT_IDLE_STATUSES = (0,)
CONTEXT_PAUSED_STATUSES = (2,)

# 影子比对的默认容差：小于它视为同一时刻（轮询粒度不同会导致必然偏差）。
_MIN_MISMATCH_TOLERANCE_MS = 500.0

# native 拉流失败后的重试间隔（避免每个等待都 fork 一次失败命令）。
_NATIVE_RETRY_BACKOFF_S = 5.0

# 最近事件环形缓冲长度（健康快照只回看最近一小段）。
_RECENT_EVENTS_MAX = 20


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in SOURCES else DEFAULT_SOURCE


def _as_shadow_sources(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return DEFAULT_SHADOW_SOURCES
    accepted: list[str] = []
    for item in value:
        source = str(item or "").strip().lower()
        if source in SOURCES and source not in accepted:
            accepted.append(source)
    return tuple(accepted)


@dataclass(frozen=True)
class EventPlaneConfig:
    """事件面配置（来自 config.py 的 `event_plane` 段）。"""

    source: str = DEFAULT_SOURCE
    shadow_sources: tuple[str, ...] = DEFAULT_SHADOW_SOURCES
    poll_interval_ms: float = DEFAULT_POLL_INTERVAL_MS
    start_grace_ms: float = DEFAULT_START_GRACE_MS
    native_cmd: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "EventPlaneConfig":
        raw = data if isinstance(data, dict) else {}
        return cls(
            source=_as_source(raw.get("source", DEFAULT_SOURCE)),
            shadow_sources=_as_shadow_sources(
                raw.get("shadow_sources", list(DEFAULT_SHADOW_SOURCES))
            ),
            poll_interval_ms=_as_float(
                raw.get("poll_interval_ms", DEFAULT_POLL_INTERVAL_MS),
                DEFAULT_POLL_INTERVAL_MS,
                1.0,
                5000.0,
            ),
            start_grace_ms=_as_float(
                raw.get("start_grace_ms", DEFAULT_START_GRACE_MS),
                DEFAULT_START_GRACE_MS,
                0.0,
                60000.0,
            ),
            native_cmd=str(raw.get("native_cmd") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "shadow_sources": list(self.shadow_sources),
            "poll_interval_ms": self.poll_interval_ms,
            "start_grace_ms": self.start_grace_ms,
            "native_cmd": self.native_cmd,
        }


DEFAULT_EVENT_PLANE = EventPlaneConfig()


def load_event_plane_config() -> EventPlaneConfig:
    """读取 config.py 的 `event_plane` 段；读不到时退回内置默认值。

    只走 config_loader（模块加载），不构造 ConfigManager —— 后者会在启动时
    发 OTA 请求，适配器初始化不应触发网络。
    """

    try:
        from core.utils import config_loader

        module = config_loader.ensure_config_module_loaded()
        app_config = getattr(module, "APP_CONFIG", None)
        section = app_config.get("event_plane") if isinstance(app_config, dict) else None
        return EventPlaneConfig.from_mapping(section)
    except Exception as exc:  # noqa: BLE001 - 配置不可读时按默认值启动
        logger.debug(
            f"[PlaybackEvents] event_plane config unavailable "
            f"({type(exc).__name__}: {exc}), using defaults"
        )
        return DEFAULT_EVENT_PLANE


def parse_native_play_status(line: str) -> int | None:
    """从 `ubus listen` 的一行输出里取出 `play_status_broadcast_event.status`。

    真实行形如 ``01:23:04 { "play_status_broadcast_event": {"status":1} }``，
    也兼容无时间戳前缀的纯 JSON。噪声行、非 JSON、非 0/1 状态一律返回 None。
    """

    if not isinstance(line, str):
        return None
    text = line.strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        data = json.loads(text[start:])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    payload = data.get("play_status_broadcast_event")
    if not isinstance(payload, dict):
        # 兼容 `ubus monitor` / 转发层常见的两种形态：
        #   {"method":"play_status_broadcast_event","data":{"status":1}}
        #   {"data":{"status":0}}
        payload = data.get("data")
    if not isinstance(payload, dict):
        payload = data
    status = payload.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if status not in (0, 1):
        return None
    return status


def _client_kind(status: Any) -> str | None:
    """client `playing` 事件的三种取值 → 适配器内部 kind。"""

    if not isinstance(status, str):
        return None
    text = status.strip().lower()
    if text in ("playing", "play"):
        return "playing"
    if text in ("paused", "pause"):
        return "paused"
    if text in ("idle", "stop", "stopped"):
        return "idle"
    return None


def _context_kind(status: Any) -> str | None:
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if status == CONTEXT_PLAYING_STATUS:
        return "playing"
    if status in CONTEXT_PAUSED_STATUSES:
        return "paused"
    if status in CONTEXT_IDLE_STATUSES:
        return "idle"
    return None


def _native_kind(status: int) -> str:
    return "playing" if status == 1 else "idle"


@dataclass
class _SourceState:
    """某个源最近一次观测（跨等待复用，用于推断「等待开始时已在播放」）。"""

    kind: str
    at: float
    seq: int = 0


@dataclass
class _SourceView:
    """一次等待里某个源的时间线（主源决定结果，影子源只记录）。"""

    source: str
    role: str
    available: bool = True
    started: bool = False
    finished: bool = False
    started_at_ms: float | None = None
    finished_at_ms: float | None = None
    idle_seen_at_ms: float | None = None
    observations: int = 0
    entry_observed: bool = False
    paused: bool = False          # 本段等待里出现过未恢复的暂停
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "available": self.available,
            "started": self.started,
            "finished": self.finished,
            "started_at_ms": _round(self.started_at_ms),
            "finished_at_ms": _round(self.finished_at_ms),
            "observations": self.observations,
            "reason": self.reason or None,
        }


class _WaitSession:
    """一次 `wait_for_finish` 的采集上下文（只保证串行语义）。"""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        views: dict[str, _SourceView],
        primary_source: str,
        started_at: float,
        timeout_s: float,
        grace_s: float,
        poll_s: float,
    ) -> None:
        self.loop = loop
        self.views = views
        self.primary_source = primary_source
        self.started_at = started_at
        self.timeout_s = timeout_s
        self.grace_s = grace_s
        self.poll_s = poll_s
        self.pending: dict[str, deque[tuple[str, float]]] = {
            source: deque() for source in views
        }
        self.signal = asyncio.Event()

    @property
    def primary(self) -> _SourceView:
        return self.views[self.primary_source]

    def enqueue(self, source: str, kind: str, at: float) -> None:
        """本事件循环内投递（context 采集任务）。"""

        bucket = self.pending.get(source)
        if bucket is None:
            return
        bucket.append((kind, at))
        self.signal.set()

    def deliver(self, source: str, kind: str, at: float) -> None:
        """跨线程投递（client 事件来自 XiaoAI.async_loop）。"""

        if source not in self.pending:
            return
        try:
            self.loop.call_soon_threadsafe(self.enqueue, source, kind, at)
        except RuntimeError:
            # 等待已结束 / 循环已关闭：丢弃即可。
            pass


class PlaybackEventAdapter:
    """播放完成事件适配器（单例经 `get_playback_events()` 获取）。

    `context_reader` 可注入 fake 源（返回 `player_get_context` 的 status 整数，
    或 None 表示读不到）；`config` 可整体替换（测试用）。
    """

    def __init__(
        self,
        context_reader: Callable[[], Awaitable[int | None]] | None = None,
        config: EventPlaneConfig | None = None,
    ) -> None:
        self.config = config or load_event_plane_config()
        self._context_reader = context_reader or self._default_context_reader
        # client/native 事件来自 XiaoAI.async_loop，等待在 MainApp.loop：
        # 状态与订阅者列表都必须用线程锁保护。
        self._state_lock = threading.Lock()
        self._states: dict[str, _SourceState] = {}
        self._seq = 0          # 单调事件序号（begin_playback / since_seq 绑定用）
        self._source_events: dict[str, int] = {source: 0 for source in SOURCES}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=_RECENT_EVENTS_MAX)
        self._sessions: list[_WaitSession] = []
        self._waits = 0
        self._mismatch_count = 0
        self._shadow_incomplete_count = 0
        self._last_result: dict[str, Any] | None = None
        self._last_mismatch: dict[str, Any] | None = None
        self._native_task: asyncio.Task | None = None
        self._native_error: str | None = None
        self._native_retry_at = 0.0
        self._native_force_at = 0.0     # 下一次允许 "立即重试" 的时刻（防 fork 风暴）
        self._native_attempts = 0       # 连续「零行即退出」的次数（指数退避）
        self._native_spawn_lines = 0    # 本次子进程产出的行数（判「无行」）
        self._native_lines = 0
        self._native_status_lines = 0   # 解析成功的状态行（噪声行不算）
        self._native_last_line_at = 0.0
        self._native_warned = False
        self._native_closing = False    # 主动 close 不算「流自己断了」

    # ---- 配置 ----

    def configure(self, **overrides: Any) -> EventPlaneConfig:
        """运行时覆盖配置（测试 seam / 后续热切换）；返回旧配置便于还原。"""

        data = self.config.as_dict()
        data.update({key: value for key, value in overrides.items() if value is not None})
        previous = self.config
        self.config = EventPlaneConfig.from_mapping(data)
        return previous

    # ---- 事件入口 ----

    def feed_client_status(self, status: Any) -> None:
        """client `playing` 事件入口（XiaoAI.on_event 调用）。"""

        kind = _client_kind(status)
        if kind is None:
            logger.debug(f"[PlaybackEvents] ignore client playing payload: {status!r}")
            return
        self._record("client", kind)

    def feed_native_line(self, line: str) -> None:
        """native `ubus listen` 行入口；噪声行直接忽略。"""

        status = parse_native_play_status(line)
        if status is None:
            return
        self._record("native", _native_kind(status))

    def begin_playback(self) -> int:
        """起播前取事件序号；把返回值传给 `wait_for_finish(since_seq=...)`。

        语义：序号之前的事件属于上一段播放，不能用来证明/结束本次播放。
        「起播与等待在同一调用栈」不构成豁免理由——`_new_view` 读的是跨调用缓存的
        最近状态，阻塞播报同样会把上一段的 playing 预成本段的起播，所以
        所有播报/播放入口都在下发命令前取号（speaker 的两条阻塞路径与 announce 一致）。
        """

        with self._state_lock:
            return self._seq

    def _record(self, source: str, kind: str) -> None:
        at = time.monotonic()
        with self._state_lock:
            self._seq += 1
            self._states[source] = _SourceState(kind=kind, at=at, seq=self._seq)
            self._source_events[source] = self._source_events.get(source, 0) + 1
            self._recent_events.append({"source": source, "kind": kind, "at": at})
            sessions = list(self._sessions)
        for session in sessions:
            session.deliver(source, kind, at)

    def _observe(self, source: str, kind: str) -> None:
        """只更新可观测性（最近事件/计数），不参与等待判定、不跨会话投递。"""

        with self._state_lock:
            self._source_events[source] = self._source_events.get(source, 0) + 1
            self._recent_events.append(
                {"source": source, "kind": kind, "at": time.monotonic()}
            )

    # ---- 等待 ----

    async def wait_for_finish(
        self,
        timeout_ms: float,
        start_grace_ms: float | None = None,
        *,
        poll_interval_ms: float | None = None,
        since_seq: int | None = None,
    ) -> Completion:
        """等待本次播报结束。

        返回 ``Completion(finished, started, reason, quality, source)``（可按
        三元组解包，兼容旧调用）：

          - ``finished``：是否可判定「本次播报已结束」。超时兜底也返回 True
            （与 ticket 03 一致：超时是完成判据的兜底，不是失败），只有在
            整个等待期间**完全读不到主源状态**时才返回 False。
          - ``started``：本次等待是否观察到主源进入「播放中」。
          - ``reason``：``idle`` / ``finished_event`` / ``idle_grace`` /
            ``timeout`` / ``shadow_finished`` / ``no_observation``。
          - ``quality``：``strong``（native 原生结束事件）或 ``weak``（其余完成）。
          - ``source``：完成事实来自哪个源。

        ``since_seq``（来自 `begin_playback()`）：序号之前的状态/事件属于上一段
        播放，不再作为「等待开始时已在播放」的证据——避免旧 paused/旧 playing
        与新 idle 拼成一次假完成。
        """

        loop = asyncio.get_running_loop()
        timeout_s = max(0.001, float(timeout_ms or 0) / 1000.0)
        grace_ms = (
            self.config.start_grace_ms
            if start_grace_ms is None
            else float(start_grace_ms)
        )
        grace_s = max(0.0, min(grace_ms, timeout_s * 1000.0) / 1000.0)
        poll_ms = (
            self.config.poll_interval_ms
            if poll_interval_ms is None
            else float(poll_interval_ms)
        )
        poll_s = max(0.001, min(poll_ms, 5000.0) / 1000.0)

        now = time.monotonic()
        primary_source, shadow_sources = self._resolve_sources()
        views: dict[str, _SourceView] = {
            primary_source: self._new_view(primary_source, "primary", now, timeout_s, since_seq)
        }
        for source in shadow_sources:
            views[source] = self._new_view(source, "shadow", now, timeout_s, since_seq)

        session = _WaitSession(
            loop=loop,
            views=views,
            primary_source=primary_source,
            started_at=now,
            timeout_s=timeout_s,
            grace_s=grace_s,
            poll_s=poll_s,
        )
        with self._state_lock:
            self._sessions.append(session)
        try:
            if primary_source == "native" or "native" in shadow_sources:
                await self._ensure_native()
            session.native_status_mark = self._native_status_lines
            result = await self._run_session(session)
        finally:
            with self._state_lock:
                try:
                    self._sessions.remove(session)
                except ValueError:
                    pass
        result = self._apply_shadow_fallback(session, result)
        view = self._completion_view(session)
        completion = self._build_completion(session, result, view)
        self._finalize_session(session, completion, view)
        return completion

    def _restart_native_stalled(self) -> None:
        """native 订阅疑似僵尸：整个等待期间零条状态行 ⇒ 主动重启，下次等待立即重连。

        只在等待以 timeout/no_observation 收尾时触发，因此不会在正常播放中途
        打断事件流（native 只在状态变化时出行，静默是正常现象）。
        """

        task = self._native_task
        self._native_retry_at = 0.0
        self._native_force_at = 0.0
        if task is None or task.done():
            return
        self._native_closing = True   # 跳过退避：下一次等待立即重连
        task.cancel()
        logger.warning(
            "[PlaybackEvents] native stream produced no status line during a "
            "whole wait; restarting subscription"
        )

    def _resolve_sources(self) -> tuple[str, list[str]]:
        source = self.config.source
        if source == "native" and not (self.config.native_cmd or "").strip():
            if not self._native_warned:
                self._native_warned = True
                logger.warning(
                    "[PlaybackEvents] native source selected without "
                    "event_plane.native_cmd; no event stream will be started"
                )
        shadows = [item for item in self.config.shadow_sources if item != source]
        return source, shadows

    def _new_view(
        self,
        source: str,
        role: str,
        now: float,
        timeout_s: float,
        since_seq: int | None = None,
    ) -> _SourceView:
        available = source != "native" or bool((self.config.native_cmd or "").strip())
        view = _SourceView(source=source, role=role, available=available)
        if source == "context":
            # context 每次等待都重新轮询，不复用历史状态（避免旧快照误判）。
            return view
        with self._state_lock:
            state = self._states.get(source)
        if state is None:
            return view
        if since_seq is not None and state.seq <= since_seq:
            # 序号之前的事件属于上一段播放：不能当作本次「已在播放」。
            return view
        if (now - state.at) * 1000.0 > timeout_s * 1000.0:
            return view
        view.entry_observed = True
        if state.kind == "playing":
            view.started = True
            view.started_at_ms = 0.0
        elif state.kind == "paused":
            # paused 只说明链路曾有内容，**不等于本次已起播**：旧 paused 与新
            # idle 拼在一起会造出一次假完成（B-03）。
            pass
        else:
            view.idle_seen_at_ms = 0.0
        return view

    async def _run_session(
        self, session: _WaitSession
    ) -> tuple[bool, bool, str]:
        tasks: list[asyncio.Task] = []
        if "context" in session.views:
            tasks.append(asyncio.ensure_future(self._collect_context(session)))
        try:
            while True:
                self._drain(session)
                outcome = self._evaluate(session)
                if outcome is not None:
                    return outcome
                now = time.monotonic()
                remaining = session.timeout_s - (now - session.started_at)
                wait_s = min(session.poll_s, max(remaining, 0.001))
                try:
                    await asyncio.wait_for(session.signal.wait(), wait_s)
                except asyncio.TimeoutError:
                    pass
                session.signal.clear()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _collect_context(self, session: _WaitSession) -> None:
        """context 源：按 poll_interval 独立轮询（与主等待解耦，不阻塞判定）。"""

        last_kind: str | None = None
        while True:
            status = await self._read_context_status()
            kind = _context_kind(status)
            if kind is not None and kind != last_kind:
                last_kind = kind
                self._observe("context", kind)
                session.enqueue("context", kind, time.monotonic())
            await asyncio.sleep(session.poll_s)

    async def _read_context_status(self) -> int | None:
        try:
            status = await self._context_reader()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读不到状态只能是「无观测」
            logger.debug(
                f"[PlaybackEvents] context read failed: {type(exc).__name__}: {exc}"
            )
            return None
        if isinstance(status, bool) or not isinstance(status, int):
            return None
        return status

    async def _default_context_reader(self) -> int | None:
        from core.device import get_device

        res = await get_device().player_context()
        if not res.ok or not isinstance(res.readback, dict):
            return None
        return res.readback.get("status")

    @staticmethod
    def _drain(session: _WaitSession) -> None:
        for source, view in session.views.items():
            bucket = session.pending[source]
            while bucket:
                kind, at = bucket.popleft()
                PlaybackEventAdapter._apply(session, view, kind, at)

    @staticmethod
    def _apply(
        session: _WaitSession, view: _SourceView, kind: str, at: float
    ) -> None:
        rel = round((at - session.started_at) * 1000.0, 1)
        view.observations += 1
        if kind == "playing":
            view.paused = False
            if not view.started:
                view.started = True
                view.started_at_ms = rel
                # 乱序（idle 先到、playing 后到）：以 playing 为准重新计时宽限。
                view.idle_seen_at_ms = None
        elif kind == "paused":
            # paused 不算结束；它只说明播放链路曾经开始过。**但暂停之后没有恢复**
            # 就转 idle，多半是用户暂停/被顶掉，不能当作「播完」（B-03）。
            view.paused = True
            if not view.started:
                view.started = True
                view.started_at_ms = rel
        elif kind == "idle":
            if view.started:
                if not view.finished:
                    view.finished = True
                    view.finished_at_ms = rel
                    view.reason = (
                        "finished_event" if view.source == "native" else "idle"
                    )
            elif view.idle_seen_at_ms is None:
                view.idle_seen_at_ms = rel

    @staticmethod
    def _apply_shadow_fallback(
        session: _WaitSession, result: tuple[bool, bool, str]
    ) -> tuple[bool, bool, str]:
        """主源完全无观测、影子源却看到完整 start→idle 时，不判失败。

        native 流可能僵尸/退避；client 影子仍能证明本次播放已起播并结束。
        只在 primary ``no_observation`` 时兜底，且要求影子 started+finished，
        避免把无关的 idle 当完成。
        """

        finished, _started, reason = result
        if finished or reason != "no_observation":
            return result
        primary = session.primary
        for view in session.views.values():
            if view is primary or not view.available:
                continue
            if view.started and view.finished:
                return True, True, "shadow_finished"
        return result

    @staticmethod
    def _completion_view(session: _WaitSession) -> _SourceView | None:
        """哪条时间线构成了「完成」：主源优先，否则影子源的完整 start→finish。"""

        primary = session.primary
        if primary.finished:
            return primary
        return next(
            (
                view
                for view in session.views.values()
                if view is not primary and view.available and view.started and view.finished
            ),
            None,
        )

    @classmethod
    def _build_completion(
        cls, session: _WaitSession, result: tuple[bool, bool, str],
        view: "_SourceView | None",
    ) -> Completion:
        finished, started, reason = result
        primary = session.primary
        if not finished:
            return Completion(False, started, reason, None, None, primary.paused)
        if view is None:
            # timeout 等无完成视图的情形：弱证据，且没有事件源真正「完成」过
            return Completion(True, started, reason, "weak", None, primary.paused)
        # strong ⇔ 主源亲自看到起播 + 结束；影子兜底/宽限兜底都是 weak
        strong = (
            view is primary
            and view.started
            and view.reason in ("idle", "finished_event")
        )
        return Completion(
            True, started, reason, "strong" if strong else "weak",
            view.source, view.paused,
        )

    @staticmethod
    def _evaluate(session: _WaitSession) -> tuple[bool, bool, str] | None:
        now = time.monotonic()
        elapsed = now - session.started_at
        primary = session.primary

        if primary.finished:
            return True, primary.started, primary.reason or "idle"

        if elapsed >= session.timeout_s:
            if primary.observations > 0 or primary.entry_observed:
                primary.reason = "timeout"
                return True, primary.started, "timeout"
            primary.reason = "no_observation"
            return False, primary.started, "no_observation"

        # 从未观察到 playing，但已看到空闲：宽限期后按「短音频已播完」处理。
        if (
            not primary.started
            and primary.idle_seen_at_ms is not None
            and elapsed >= session.grace_s
        ):
            primary.finished = True
            primary.finished_at_ms = round(elapsed * 1000.0, 1)
            primary.reason = "idle_grace"
            return True, False, "idle_grace"
        return None

    # ---- 影子比对与快照 ----

    def _finalize_session(
        self, session: _WaitSession, result: Completion,
        completion: "_SourceView | None",
    ) -> None:
        finished, started, reason = result.finished, result.started, result.reason
        elapsed_ms = round((time.monotonic() - session.started_at) * 1000.0, 1)
        if (
            session.primary_source == "native"
            and reason in ("timeout", "no_observation")
            and self._native_status_lines == session.native_status_mark
            and self._native_running()
        ):
            self._restart_native_stalled()
        primary = session.primary
        if not primary.reason:
            # 主源没结束而影子兜底成功时，不能把 primary 标成 finished_event。
            primary.reason = reason if primary.finished else "no_observation"

        for view in session.views.values():
            if view.reason:
                continue
            if view.finished:
                view.reason = "idle"
            elif view.observations or view.entry_observed:
                view.reason = "not_finished"
            elif not view.available:
                view.reason = "unavailable"
            else:
                view.reason = "no_observation"

        timelines = {source: view.as_dict() for source, view in session.views.items()}
        completion_source = result.source
        completion_quality = result.quality
        record = {
            "source": primary.source,
            "completion_source": completion_source,
            "completion_quality": completion_quality,
            "finished": finished,
            "started": started,
            "reason": reason,
            "paused": result.paused,
            "elapsed_ms": elapsed_ms,
            "timelines": timelines,
        }

        tolerance = max(_MIN_MISMATCH_TOLERANCE_MS, session.poll_s * 2000.0)
        mismatches: list[dict[str, Any]] = []
        incomplete = 0
        for view in session.views.values():
            if view is primary or not view.available:
                continue
            kinds: list[str] = []
            deltas: dict[str, float] = {}
            if primary.started != view.started:
                kinds.append("started_flag")
            if (
                primary.started
                and view.started
                and primary.started_at_ms is not None
                and view.started_at_ms is not None
            ):
                deltas["started_ms"] = round(
                    view.started_at_ms - primary.started_at_ms, 1
                )
                if abs(deltas["started_ms"]) > tolerance:
                    kinds.append("started_delta")
            if (
                primary.finished
                and view.finished
                and primary.finished_at_ms is not None
                and view.finished_at_ms is not None
            ):
                deltas["finished_ms"] = round(
                    view.finished_at_ms - primary.finished_at_ms, 1
                )
                if abs(deltas["finished_ms"]) > tolerance:
                    kinds.append("finished_delta")
            elif primary.finished:
                # 主源提前结束、影子没来得及看到结束：是采集窗口限制，不计不一致。
                incomplete += 1
                view.reason = view.reason or "incomplete"
            if kinds:
                mismatches.append(
                    {"source": view.source, "kinds": kinds, "deltas": deltas}
                )

        with self._state_lock:
            self._waits += 1
            self._last_result = record
            if mismatches:
                self._mismatch_count += len(mismatches)
                self._last_mismatch = {
                    "source": primary.source,
                    "primary": record["reason"],
                    "mismatches": mismatches,
                }
            if incomplete:
                self._shadow_incomplete_count += incomplete

        # TurnTrace（ticket 11）：把本轮播放 started/finished 落进当前轮的绝对时间线
        # （仅收尾已进入 TTS 合成的轮；外部播报不会误收尾 LLM 轮）。
        turn_trace.note_playback(
            finished=finished,
            started=started,
            reason=reason,
            wait_started_at=session.started_at,
            started_at_ms=(
                completion.started_at_ms if completion else primary.started_at_ms
            ),
            finished_at_ms=(
                completion.finished_at_ms if completion else primary.finished_at_ms
            ),
        )

        logger.debug(
            "[PlaybackEvents] wait " + json.dumps(record, ensure_ascii=False)
        )
        if mismatches:
            logger.warning(
                "[PlaybackEvents] shadow mismatch primary="
                f"{primary.source} " + json.dumps(mismatches, ensure_ascii=False)
            )

    def snapshot(self) -> dict[str, Any]:
        """健康/排障快照：主源、影子、最近事件、不一致计数与上一轮时间线。"""

        now = time.monotonic()
        with self._state_lock:
            states = {
                source: {
                    "kind": state.kind,
                    "age_ms": round((now - state.at) * 1000.0, 1),
                }
                for source, state in self._states.items()
            }
            recent = [
                {
                    "source": item["source"],
                    "kind": item["kind"],
                    "age_ms": round((now - item["at"]) * 1000.0, 1),
                }
                for item in self._recent_events
            ]
            return {
                "source": self.config.source,
                "shadow_sources": list(self.config.shadow_sources),
                "poll_interval_ms": self.config.poll_interval_ms,
                "start_grace_ms": self.config.start_grace_ms,
                "native_cmd_configured": bool(
                    (self.config.native_cmd or "").strip()
                ),
                "native_running": self._native_running(),
                "native_ready": self._native_running(),
                "native_error": self._native_error,
                "native_attempts": self._native_attempts,
                "native_lines": self._native_lines,
                "native_status_lines": self._native_status_lines,
                "native_last_line_age_ms": (
                    _round((now - self._native_last_line_at) * 1000.0)
                    if self._native_last_line_at
                    else None
                ),
                "waits": self._waits,
                "mismatch_count": self._mismatch_count,
                "shadow_incomplete_count": self._shadow_incomplete_count,
                "last_result": self._last_result,
                "last_mismatch": self._last_mismatch,
                "recent_events": recent,
                "event_counts": dict(self._source_events),
                "sources": states,
            }

    def _native_running(self) -> bool:
        task = self._native_task
        return bool(task is not None and not task.done())

    # ---- native 拉流（默认关闭，仅在配置 native_cmd 时启动）----

    async def prepare(self, ready_timeout_ms: float = 200.0) -> bool:
        """起播前预启动 native 流，并给出**有界**就绪确认。

        就绪判据 = 子进程在短窗口内仍然存活（`ubus listen` 只在事件时出流，
        没有输出不等于没就绪；进程立刻退出才是没就绪）。这样「订阅就绪」不再
        只能靠第一条播放事件反推（B-03）。非 native 主源/影子时 no-op。
        """

        source, shadows = self._resolve_sources()
        if source != "native" and "native" not in shadows:
            return False
        if not await self._ensure_native(force=True):
            return False
        budget = max(0.0, min(float(ready_timeout_ms), 2000.0)) / 1000.0
        await asyncio.sleep(min(0.02, budget))
        return self._native_running()

    async def _ensure_native(self, force: bool = False) -> bool:
        cmd = (self.config.native_cmd or "").strip()
        if not cmd:
            return False
        if self._native_running():
            return True
        now = time.monotonic()
        if force and now >= self._native_force_at:
            # 退避窗口内允许一次立即尝试；之后必须等退避，避免每次播报都 fork
            self._native_retry_at = 0.0
            self._native_force_at = now + self._native_backoff()
        if now < self._native_retry_at:
            return False
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001 - 启动失败只记录，退回超时兜底
            self._native_error = f"{type(exc).__name__}: {exc}"
            self._native_retry_at = now + _NATIVE_RETRY_BACKOFF_S
            logger.warning(
                f"[PlaybackEvents] native event stream start failed: "
                f"{self._native_error}"
            )
            return False
        self._native_error = None
        self._native_spawn_lines = 0
        self._native_task = asyncio.ensure_future(self._read_native(proc))
        logger.info("[PlaybackEvents] native event stream started")
        return True

    def _native_backoff(self) -> float:
        """零行退出的指数退避（5s → 10s → … 上限 60s）。"""

        return min(_NATIVE_RETRY_BACKOFF_S * (2 ** self._native_attempts), 60.0)

    async def _read_native(self, proc: Any) -> None:
        stdout = getattr(proc, "stdout", None)
        try:
            while stdout is not None:
                line = await stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace")
                self._native_lines += 1
                self._native_spawn_lines += 1
                self._native_last_line_at = time.monotonic()
                if parse_native_play_status(text) is None:
                    continue   # 噪声行证明进程活着，但不能抵消「断流」计数
                self._native_attempts = 0
                self._native_status_lines += 1
                self.feed_native_line(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 拉流异常不得影响播报
            self._native_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if getattr(proc, "returncode", None) is None:
                    proc.terminate()
            except ProcessLookupError:
                pass
            except Exception:  # noqa: BLE001
                pass
            self._native_task = None
            intentional = self._native_closing
            self._native_closing = False
            if intentional:
                return
            if self._native_spawn_lines == 0:
                # 一行都没有：命令/网络不对，按指数退避，别每次播报都重 fork
                self._native_attempts += 1
            # 先按「本次之前的连续失败次数」算退避，再加一 —— 首次是 5s 不是 10s
            backoff = self._native_backoff()
            self._native_retry_at = time.monotonic() + backoff
            logger.warning(
                "[PlaybackEvents] native event stream ended "
                f"(lines={self._native_spawn_lines}, attempts={self._native_attempts}, "
                f"retry_in={backoff:.0f}s); completion falls back to timeout"
            )

    async def close(self) -> None:
        """停止 native 拉流（测试/关停用）。"""

        task = self._native_task
        if task is None:
            return
        self._native_closing = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


_DEFAULT_ADAPTER: PlaybackEventAdapter | None = None


def get_playback_events() -> PlaybackEventAdapter:
    """取全局适配器单例（首次调用时读取 `event_plane` 配置）。"""

    global _DEFAULT_ADAPTER
    if _DEFAULT_ADAPTER is None:
        _DEFAULT_ADAPTER = PlaybackEventAdapter()
    return _DEFAULT_ADAPTER


def set_playback_events(adapter: PlaybackEventAdapter) -> None:
    """测试/运行时可替换单例（注入 fake 源）。"""

    global _DEFAULT_ADAPTER
    _DEFAULT_ADAPTER = adapter


def reset_playback_events() -> None:
    """测试辅助：丢弃单例（不负责关闭 native 子进程）。"""

    global _DEFAULT_ADAPTER
    _DEFAULT_ADAPTER = None
