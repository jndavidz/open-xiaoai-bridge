"""TTS/音乐互斥编排器（pcm-backend spec 票 02，本 spec 核心需求）。

spike 耳测实锤（pending-followups §1.11）：PCM 推流（aplay/dmix）与固件 TTS
（miplayer→mipns）双时钟源并发混音出噪音。编排器把原厂 tts_play.sh 的
「pause→TTS→play」互斥模式搬到桥侧：

  - TTS 起播前（经由 PcmBackend 暴露的 pause）自动停推音乐并记忆偏移；
  - TTS 结束（focus 归还 → resume）后从记忆偏移 start_ms 续推；
  - 曲末由 native 会话终态判定（completed），桥侧回调通知，不依赖设备事件；
  - 超时兜底：暂停态挂看门狗，TTS focus 归零仍 paused ⇒ 自动续推
    （focus 拒绝播报等路径不得永久哑音乐）；推流停滞自愈（有界重试）。

native 访问经注入（`PcmNativeAdapter` 为生产默认，测试注入 fake）。
位置口径：position = actual_start_ms + pushed_ms（seek 落点 + 本次已推），
误差受 aplay 缓冲（~200ms）限制，满足 spec 验收 ≤2s。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

from core.utils.logger import logger

_TERMINAL_STATUS = ("completed", "cancelled", "failed")


class PcmNativeError(RuntimeError):
    """native 推流调用失败（起播被拒等）。"""


class PcmNativeAdapter:
    """生产 native 适配：open_xiaoai_server 的 pcm_stream_* 三件套。"""

    async def start(self, source: str, start_ms: int) -> int:
        import open_xiaoai_server as native

        return int(await native.pcm_stream_start(source, start_ms=start_ms))

    def stop(self, token: int | None) -> None:
        import open_xiaoai_server as native

        native.pcm_stream_stop(token)

    def state(self, token: int) -> dict | None:
        import open_xiaoai_server as native

        raw = native.pcm_session_state(token)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001 - 状态损坏按未知处理
            return None
        return data if isinstance(data, dict) else None


def _default_focus_active() -> bool:
    """TTS focus 是否持有中（hold/待释放/令牌任一在即算）。

    编排器只读；focus 状态机归 focus_bridge 所有。
    """

    from core.music import focus_bridge

    return bool(
        getattr(focus_bridge, "_hold_count", 0) > 0
        or getattr(focus_bridge, "_hold_token", None) is not None
        or getattr(focus_bridge, "_pending_release", None) is not None
    )


class PcmOrchestrator:
    """单会话互斥编排：至多一条音乐推流，状态 playing / paused / idle。"""

    def __init__(
        self,
        *,
        native=None,
        clock: Callable[[], float] = time.monotonic,
        focus_active: Callable[[], bool] | None = None,
        watchdog_timeout_s: float = 120.0,
        stall_s: float = 10.0,
        terminal_poll_s: float = 0.05,
        terminal_timeout_s: float = 1.0,
        max_heal_retries: int = 3,
    ) -> None:
        self._native = native or PcmNativeAdapter()
        self._clock = clock
        self._focus_active = focus_active or _default_focus_active
        self._watchdog_timeout_s = max(0.0, float(watchdog_timeout_s))
        self._stall_s = max(1.0, float(stall_s))
        self._terminal_poll_s = max(0.01, float(terminal_poll_s))
        self._terminal_timeout_s = max(0.05, float(terminal_timeout_s))
        self._max_heal_retries = max(1, int(max_heal_retries))

        self._state = "idle"
        self._session: dict | None = None
        self._watchdog: asyncio.Task | None = None
        self._healing = False
        # 曲末/推流死亡回调（票 04 auto_advance 由 backend/runtime 消费）
        self.on_track_end: Callable[[str], None] | None = None

    # ---- 查询（供 backend） ----

    def state(self) -> str:
        return self._state

    def position_ms(self) -> int | None:
        if self._session is None:
            return None
        return int(self._session.get("position", 0))

    def duration_ms(self) -> int | None:
        if self._session is None:
            return None
        return self._session.get("duration_ms")

    # ---- 会话生命周期 ----

    async def start(self, source: str, start_ms: int = 0) -> None:
        """起播/续推一条音乐源；成功后编排器处于 playing。"""

        self._cancel_watchdog()
        token = await self._native.start(str(source), int(start_ms))
        self._session = {
            "token": int(token),
            "source": str(source),
            "position": int(start_ms),
            "last_progress": self._clock(),
            "heals": 0,
            "duration_ms": None,
        }
        self._state = "playing"
        logger.info(f"[PcmOrchestrator] start token={token} start_ms={start_ms}")

    async def pause(self) -> int | None:
        """停推并记忆偏移（TTS 起播前由 backend.pause 驱动）。返回记忆位置。"""

        self._cancel_watchdog()
        if self._session is None or self._state != "playing":
            return self.position_ms()
        token = self._session["token"]
        self._native.stop(token)
        rec = await self._await_terminal(token)
        if rec is not None:
            pos = int(rec.get("actual_start_ms") or 0) + int(rec.get("pushed_ms") or 0)
            duration = rec.get("duration_ms")
            if duration:
                self._session["duration_ms"] = int(duration)
        else:
            # 终态读取失败（会话已清/注册表丢）：退回最后已知进度
            pos = self.position_ms() or 0
            logger.warning("[PcmOrchestrator] pause: terminal state unreadable, "
                           f"falling back to last known position {pos}")
        self._session["position"] = pos
        self._state = "paused"
        self._arm_watchdog()
        logger.info(f"[PcmOrchestrator] paused at {pos} ms")
        return pos

    async def resume(self) -> int | None:
        """从记忆偏移续推（focus 归还 / 用户继续播放）。返回起推偏移。"""

        if self._session is None:
            return None
        self._cancel_watchdog()
        offset = int(self._session["position"])
        token = await self._native.start(self._session["source"], offset)
        self._session["token"] = int(token)
        self._session["last_progress"] = self._clock()
        self._state = "playing"
        logger.info(f"[PcmOrchestrator] resumed from {offset} ms (token={token})")
        return offset

    async def seek_to(self, pos_ms: int) -> None:
        """桥侧重投：停推并从指定偏移重新起推（无活跃会话则明拒）。"""

        if self._session is None:
            raise PcmNativeError("no active pcm session")
        if self._state == "playing":
            await self.pause()
        self._session["position"] = max(0, int(pos_ms))
        await self.resume()

    async def stop(self) -> None:
        """显式停止：停推并丢弃会话（换曲/用户停止/队列结束）。"""

        self._cancel_watchdog()
        if self._session is not None and self._state == "playing":
            self._native.stop(self._session["token"])
        self._session = None
        self._state = "idle"

    # ---- 进度快照（非阻塞，由 backend.position()/state() 周期驱动） ----

    def refresh(self) -> None:
        """同步快照：推进位置、检测曲末/死亡，必要时安排自愈。"""

        if self._session is None or self._state != "playing" or self._healing:
            return
        rec = self._native.state(self._session["token"])
        if rec is None:
            return
        status = str(rec.get("status") or "")
        actual = int(rec.get("actual_start_ms") or 0)
        pushed = int(rec.get("pushed_ms") or 0)
        pos = actual + pushed
        if rec.get("duration_ms"):
            self._session["duration_ms"] = int(rec["duration_ms"])

        if status == "completed":
            self._finish("completed", pos)
            return
        if status in ("cancelled", "failed"):
            # token 被 TTS 抢走（推流失效）或推流失败：位置停在已知进度，
            # 走停滞自愈；focus 持有中则保持安静（TTS 优先），由 focus 归还续推。
            if self._focus_active():
                self._session["position"] = max(pos, self._session["position"])
                self._state = "paused"
                self._arm_watchdog()
                logger.info("[PcmOrchestrator] push lost while TTS holds focus; "
                            f"held at {pos} ms")
                return
            self._schedule_heal(pos)
            return

        # starting/playing：位置单调前进
        if pos > self._session["position"]:
            self._session["position"] = pos
            self._session["last_progress"] = self._clock()
        elif self._clock() - self._session["last_progress"] > self._stall_s:
            self._schedule_heal(pos)

    def _finish(self, reason: str, pos: int) -> None:
        self._session["position"] = pos
        self._session = None
        self._state = "idle"
        logger.info(f"[PcmOrchestrator] session ended ({reason}) at {pos} ms")
        if self.on_track_end is not None:
            try:
                self.on_track_end(reason)
            except Exception as exc:  # noqa: BLE001 - 回调异常不杀编排器
                logger.warning(f"[PcmOrchestrator] on_track_end failed: {exc}")

    # ---- 自愈（推流死亡/停滞） ----

    def _schedule_heal(self, pos: int) -> None:
        if self._healing:
            return
        heals = self._session["heals"]
        if heals >= self._max_heal_retries:
            logger.error("[PcmOrchestrator] heal budget exhausted; giving up session")
            self._finish("lost", pos)
            return
        self._session["position"] = max(pos, self._session["position"])
        self._healing = True
        asyncio.ensure_future(self._heal())

    async def _heal(self) -> None:
        try:
            offset = int(self._session["position"])
            self._session["heals"] += 1
            logger.warning(f"[PcmOrchestrator] push stalled; healing from {offset} ms "
                           f"(attempt {self._session['heals']})")
            token = await self._native.start(self._session["source"], offset)
            self._session["token"] = int(token)
            self._session["last_progress"] = self._clock()
        except Exception as exc:  # noqa: BLE001 - 自愈失败进下一轮或放弃
            logger.warning(f"[PcmOrchestrator] heal failed: {type(exc).__name__}: {exc}")
        finally:
            self._healing = False

    # ---- 看门狗（暂停态兜底） ----

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        if self._watchdog_timeout_s <= 0:
            return
        self._watchdog = asyncio.ensure_future(self._watchdog_run())

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    async def _watchdog_run(self) -> None:
        try:
            await asyncio.sleep(self._watchdog_timeout_s)
        except asyncio.CancelledError:
            return
        self._watchdog = None
        if self._state == "paused" and not self._focus_active():
            logger.warning("[PcmOrchestrator] watchdog: still paused without TTS "
                           "focus; resuming music")
            try:
                await self.resume()
            except Exception as exc:  # noqa: BLE001 - 兜底失败如实记录
                logger.error(f"[PcmOrchestrator] watchdog resume failed: {exc}")

    # ---- 内部 ----

    async def _await_terminal(self, token: int) -> dict | None:
        # 有界结算用事件循环真实时间：注入的 playback clock 可能是冻结的假时钟
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._terminal_timeout_s
        while loop.time() < deadline:
            rec = self._native.state(token)
            if rec is not None and str(rec.get("status") or "") in _TERMINAL_STATUS:
                return rec
            await asyncio.sleep(self._terminal_poll_s)
        return None
