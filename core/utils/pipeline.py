"""对话管线态、轮次标识与取消传播（P1 ticket 02：流式化地基，行为等价）。

分层约束（与协议态并存，不重构协议态）：

    - 管线态只描述对话循环内部的「听 → 处理 → 播」阶段（idle/listening/
      processing/speaking），由对话循环按固定位置迁移并打迁移日志；
    - MainApp 的 device_state 是设备协议态（发给小智协议），语义与发送路径
      不受本模块影响；两者并行存在，管线态不反向驱动协议态。

轮次（turn）：

    - 每轮对话（进入监听 → ASR 定稿 → LLM → TTS 播完）分配一个全局递增的
      turn_id，贯穿对话循环日志与 TurnTrace 延迟埋点（TurnTrace 建轮时复用
      本模块的号，保证日志行与 `[TurnTrace]` 行的轮次同源）。
    - bridge 单设备、轮次串行：同一时刻至多一个活跃轮；新轮开始时旧轮自动
      置为 superseded（防旧协程漏网继续播）。

取消传播：

    - PipelineTurn.cancel() 置取消标志（幂等，首次返回 True）；
    - 对话循环在固定检查点（ASR 定稿后 / LLM 请求前 / 播放等待内）调
      checkpoint()：已取消即抛 TurnCancelledError，本轮静默退出——不再发
      LLM 请求、不再播后续句段、不打 ERROR 日志；
    - 打断 = 唤醒事件置取消（wakeup_session.on_interrupt → cancel_all_turns）
      + 既有播放令牌定向杀流（stop_tts_playback），两层配合，既有行为不变。

线程模型：与 TurnTrace 相同——全局锁保护，可在事件线程与 asyncio 循环
两侧调用；本模块纯内存、无 I/O，任何异常不得影响对话主流程。
"""

from __future__ import annotations

import enum
import threading
import time

from core.utils.logger import logger

LOG_MODULE = "Pipeline"


class PipelineState(str, enum.Enum):
    """对话循环的管线态（与设备协议态分层并存）。"""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


# 固定检查点名（spec：ASR 定稿后 / LLM 请求前 / 播放等待内 / 流式句循环内
# 每个句段；句段检查点自 ticket 03 起由对话循环的句段消费路径使用）。
CHECKPOINT_ASR_FINAL = "asr_final"
CHECKPOINT_PRE_LLM = "pre_llm"
CHECKPOINT_PLAYBACK = "playback"
CHECKPOINT_STREAM_SEGMENT = "stream_segment"


class TurnCancelledError(Exception):
    """本轮对话已被取消：检查点抛出，调用方静默退出本轮。"""

    def __init__(self, turn_id: int, checkpoint: str, reason: str):
        self.turn_id = int(turn_id)
        self.checkpoint = str(checkpoint)
        self.reason = str(reason or "unknown")
        super().__init__(
            f"turn={turn_id} cancelled at checkpoint={checkpoint} reason={self.reason}"
        )


class PipelineTurn:
    """一轮对话的轮次标识与取消标志（纯内存，线程安全）。"""

    __slots__ = ("turn_id", "source", "started_at", "_cancelled", "cancel_reason")

    def __init__(self, turn_id: int, source: str):
        self.turn_id = int(turn_id)
        self.source = str(source or "")
        self.started_at = time.monotonic()
        self._cancelled = False
        self.cancel_reason = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self, reason: str) -> bool:
        """置取消标志；幂等，首次置位返回 True。"""
        if self._cancelled:
            return False
        self._cancelled = True
        self.cancel_reason = str(reason or "unknown")
        return True

    def checkpoint(self, name: str) -> None:
        """异步检查点：本轮已取消时抛 TurnCancelledError。"""
        if self._cancelled:
            raise TurnCancelledError(self.turn_id, name, self.cancel_reason)

    def check(self, name: str) -> bool:
        """非抛式探测：返回本轮是否可以继续（未取消）。

        供不能抛异常的调用面（如 xiaoai 事件线程的 consume 回调）使用。
        """
        if self._cancelled:
            logger.info(
                f"turn={self.turn_id} cancelled at checkpoint={name} "
                f"reason={self.cancel_reason} (swallowed)",
                module=LOG_MODULE,
            )
            return False
        return True


# ---- 全局状态（单设备、轮次串行）----

_LOCK = threading.RLock()
_turn_counter = 0
_current: PipelineTurn | None = None
_state = PipelineState.IDLE
_state_turn_id: int | None = None

# 状态订阅者（P3 票 02：灯语等可视化出口订阅管线态迁移与轮次收尾）。
# 线程模型：回调在触发线程（事件线程或 asyncio 循环）同步上抛——订阅者
# 必须快进快出、不得抛异常（通知侧隔离记 WARNING）；跨线程/协程投递由
# 订阅者自理（schedule / run_coroutine_threadsafe）。
_STATE_SUB_LOCK = threading.Lock()
_state_subscribers: list = []


def subscribe_state(callback) -> "callable":
    """订阅管线事件：callback(event, data)。

    event="state_changed"：data={"state": 新态, "turn_id": 轮次号}；
    event="turn_ended"：data={"outcome": 收尾原因, "turn_id": 轮次号}。
    返回注销函数（幂等）。
    """
    with _STATE_SUB_LOCK:
        _state_subscribers.append(callback)

    def _unsubscribe():
        with _STATE_SUB_LOCK:
            try:
                _state_subscribers.remove(callback)
            except ValueError:
                pass

    return _unsubscribe


def _notify_state(event: str, data: dict) -> None:
    """广播管线事件；单个订阅者异常被隔离记 WARNING，不影响其他人。"""
    with _STATE_SUB_LOCK:
        snapshot = list(_state_subscribers)
    for callback in snapshot:
        try:
            callback(event, data)
        except Exception as exc:
            logger.warning(
                f"state subscriber failed: {type(exc).__name__}: {exc}",
                module=LOG_MODULE,
            )


def begin_turn(source: str = "") -> PipelineTurn:
    """开始新一轮：分配 turn_id、状态迁至 listening；旧轮置 superseded。"""
    global _turn_counter, _current, _state, _state_turn_id
    with _LOCK:
        _turn_counter += 1
        turn = PipelineTurn(_turn_counter, source)
        previous = _current
        if previous is not None and previous.cancel("superseded"):
            logger.info(
                f"turn={previous.turn_id} superseded by turn={turn.turn_id}",
                module=LOG_MODULE,
            )
        _current = turn
        old_state = _state
        _state = PipelineState.LISTENING
        _state_turn_id = turn.turn_id
    logger.info(
        f"turn={turn.turn_id} state={old_state.value}->{_state.value} src={source}",
        module=LOG_MODULE,
    )
    _notify_state(
        "state_changed", {"state": _state.value, "turn_id": turn.turn_id}
    )
    return turn


def current_turn() -> PipelineTurn | None:
    with _LOCK:
        return _current


def current_turn_id() -> int | None:
    with _LOCK:
        return _current.turn_id if _current is not None else None


def cancel_current_turn(reason: str) -> bool:
    """取消当前轮（唤醒打断 / 会话复位用）；无活跃轮返回 False。"""
    with _LOCK:
        turn = _current
    if turn is None:
        return False
    return _cancel_turn(turn, reason)


def cancel_all_turns(reason: str) -> int:
    """取消全部活跃轮（单设备串行 ⇒ 至多 1 个）；返回取消数。"""
    with _LOCK:
        turn = _current
    if turn is None or not _cancel_turn(turn, reason):
        return 0
    return 1


def _cancel_turn(turn: PipelineTurn, reason: str) -> bool:
    first = turn.cancel(reason)
    if first:
        logger.info(
            f"turn={turn.turn_id} cancelled reason={reason}",
            module=LOG_MODULE,
        )
    return first


def end_turn(outcome: str = "done", *, turn: PipelineTurn | None = None) -> None:
    """收尾当前轮：状态迁回 idle。

    turn：调用方持有的轮引用（对话循环 finally 用）——仅当它仍是当前活跃轮
    时才收尾，避免晚到的旧循环 finally 误杀已被打断方收掉或被新轮取代的轮。
    无当前轮或引用不匹配时静默。
    """
    global _current, _state, _state_turn_id
    with _LOCK:
        if turn is not None and _current is not turn:
            return
        current = _current
        _current = None
        if current is None:
            return
        old_state = _state
        _state = PipelineState.IDLE
        _state_turn_id = None
    logger.info(
        f"turn={current.turn_id} state={old_state.value}->idle outcome={outcome}",
        module=LOG_MODULE,
    )
    _notify_state(
        "turn_ended", {"outcome": outcome, "turn_id": current.turn_id}
    )


def set_state(state: PipelineState, note: str = "") -> None:
    """管线态迁移并打迁移日志（带轮次号）；迁移到相同状态时静默。"""
    global _state, _state_turn_id
    if not isinstance(state, PipelineState):
        raise TypeError(f"state must be PipelineState, got {type(state)!r}")
    with _LOCK:
        turn_id = _current.turn_id if _current is not None else _state_turn_id
        if state is _state:
            return
        old_state = _state
        _state = state
        _state_turn_id = turn_id
    suffix = f" {note}" if note else ""
    logger.info(
        f"turn={turn_id if turn_id is not None else '-'} "
        f"state={old_state.value}->{state.value}{suffix}",
        module=LOG_MODULE,
    )
    _notify_state("state_changed", {"state": state.value, "turn_id": turn_id})


def state() -> PipelineState:
    with _LOCK:
        return _state


def snapshot() -> dict:
    """观测快照（测试与排障；不进协议面）。"""
    with _LOCK:
        turn = _current
        return {
            "state": _state.value,
            "turn": turn.turn_id if turn else None,
            "turn_source": turn.source if turn else "",
            "cancelled": bool(turn.cancelled) if turn else False,
            "cancel_reason": turn.cancel_reason if turn else "",
        }


def reset_pipeline() -> None:
    """测试辅助：清空全局轮次、状态与订阅表（不打日志）。"""
    global _turn_counter, _current, _state, _state_turn_id
    with _LOCK:
        _turn_counter = 0
        _current = None
        _state = PipelineState.IDLE
        _state_turn_id = None
    with _STATE_SUB_LOCK:
        _state_subscribers.clear()
