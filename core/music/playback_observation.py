"""设备侧播放观测原语（架构收敛，2026-11-26）。

只做一件事：把「命令回执」与「已起播」分离——回执 OK 只证明命令受理，
起播真相以 worker 上报的 STATE play + 新鲜接收时间为准（复核 F2）。

事务状态与受控判据已移入 `playback_session.PlaybackSession`（per renderer），
本模块保持无状态，纯函数便于单测。
"""
from __future__ import annotations

import asyncio
import time

from core.music import cch_np
from core.utils.logger import logger


async def observe_play(timeout_s: float = 3.0, poll_s: float = 0.15,
                       since_ts: float | None = None) -> bool:
    """有界等待设备侧新鲜 play 观测；拿不到返回 False（fail-closed）。

    since_ts 给定后只认该时刻之后到达的 play（过滤上一首遗留状态）。
    """
    deadline = time.time() + timeout_s
    since = since_ts if since_ts is not None else 0.0
    while True:
        snap = cch_np.snapshot()
        st = snap.get("state") or {}
        extras = st.get("extras") or {}
        status = str(extras.get("statusStr") or "").lower()
        observed_at = float(st.get("observed_at") or 0.0)
        if status == "play" and observed_at >= since:
            logger.debug("[PlaybackObs] fresh play observed", module="PlaybackObs")
            return True
        if time.time() >= deadline:
            logger.info(f"[PlaybackObs] no fresh play within {timeout_s:g}s "
                        f"last={status!r}", module="PlaybackObs")
            return False
        await asyncio.sleep(poll_s)
