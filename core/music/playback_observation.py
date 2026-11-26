"""CCH 播放观测契约（架构收敛，2026-11-26）。

单一职责：把「设备侧 observable」与「我方命令回执」分离——
命令回执 OK 只证明命令受理，不等于已起播（复核 F2）；起播真相以
worker 上报的 STATE play + 新鲜接收时间为准。

对外两个 seam：
- `begin(entry, budget_s)` / `end()` / `active()`：一次点播事务的边界与静默判据。
  FREEZE observer 只读 `active()`，**不再**依赖 focus/pipeline/voice 三道猜测门。
- `observe_play(entry, timeout_s)`：有界轮询设备状态，确认本次起播。

不变量：观测只认事务开始后新到达的 play 事件（`observed_at >= since_ts`），
旧状态（上一首遗留的 play）不算本次确认。
"""
from __future__ import annotations

import asyncio
import time

from core.music import cch_np
from core.utils.logger import logger

_state = {"active_until": 0.0, "entry": "", "since_ts": 0.0}


def begin(entry: str, budget_s: float = 12.0) -> float:
    """开始一次点播事务，返回相位起点（供观测过滤旧状态）。"""
    now = time.time()
    _state["active_until"] = now + budget_s
    _state["entry"] = str(entry)
    _state["since_ts"] = now
    return now


def end() -> None:
    _state["active_until"] = 0.0


def active() -> bool:
    """事务是否活跃（FREEZE/换代 observer 的唯一静默判据）。"""
    return time.time() < _state["active_until"]


def entry() -> str:
    return _state["entry"]


def suppressed() -> bool:
    """系统是否处于受控态（FREEZE observer 的唯一判据）。

    事务进行中（我方正在点播）或 TTS focus 持有中（对话正在管理恢复资格）
    都不接受 FREEZE 恢复——两类情形由各自机制负责，观测层只处理无人接管
    的真实冻结。取代散落在 cch_click 的 focus/pipeline/voice 三道猜测门。
    """
    if active():
        return True
    try:
        from core.music import focus_bridge

        return focus_bridge.focus_holding()
    except Exception:  # noqa: BLE001 - 观测层失败关闭（回报受控，不擅自恢复）
        return True


async def observe_play(timeout_s: float = 3.0, poll_s: float = 0.15) -> bool:
    """有界等待设备侧新鲜 play 观测；不拿到就返回 False（fail-closed）。"""
    deadline = time.time() + timeout_s
    since = _state["since_ts"]
    while True:
        snap = cch_np.snapshot()
        st = snap.get("state") or {}
        extras = st.get("extras") or {}
        status = str(extras.get("statusStr") or "").lower()
        observed_at = float(st.get("observed_at") or 0.0)
        if status == "play" and observed_at >= since:
            logger.debug(f"[PlaybackObs] observed play entry={_state['entry']}",
                         module="PlaybackObs")
            return True
        if time.time() >= deadline:
            logger.info(f"[PlaybackObs] no fresh play within {timeout_s:g}s "
                        f"last={status!r} observed_at={observed_at:.3f} since={since:.3f}",
                        module="PlaybackObs")
            return False
        await asyncio.sleep(poll_s)
