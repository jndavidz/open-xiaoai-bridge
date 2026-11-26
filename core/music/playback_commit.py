"""CCH 点播提交点（架构收敛，2026-11-26）。

一次点播的四件事——设备起播、所有权、呈现、投影——在这里收成唯一提交点，
取代散落在 song_request 各处的 if/常量补丁。

三条不变量（tests/test_playback_commit.py 守门）：
1. 起播真相由设备观测裁决（playback_observation.observe_play），命令回执只作参考；
   回执 TIMEOUT 但设备已起播仍算成功（真机反复出现的形态）。
2. 所有权 `_cch_state` 只在本模块原子提交一次；调用方取消不会留下半状态。
3. 呈现层（曲名/封面/歌词）是不可取消的后台任务，调用方（LLM tool）超时
   不再让 UI/歌词停留旧曲。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.utils.logger import logger


@dataclass
class CommitResult:
    ok: bool
    detail: str
    presentation: asyncio.Task | None = None


async def commit_playback(
    *,
    play_url: str,
    item: dict,
    items: list[dict],
    meta: dict,
    bg_url: str,
    intent: str,
    announce_task: asyncio.Task | None = None,
) -> CommitResult:
    """提交一次点播：观测裁决 → 原子提交所有权 → 不可取消的呈现/投影。"""
    from core.music import cch_cast, playback_observation as obs
    from core.tools import song_request as sr

    if announce_task is not None:
        await announce_task
    # 事务边界由调用方（song_request 的 play 意图）提前 begin，覆盖取链全程；
    # 这里只负责收尾 end，避免 FREEZE 在取票窗口内抢回旧曲。
    try:
        # 起播前幂等 stop（TTS suspend/resume 可能唤醒旧会话）
        try:
            await cch_cast.host_cmd_nowait("stop")
        except Exception:  # noqa: BLE001
            pass
        ok, status = await cch_cast.host_cmd("play", url=play_url)
        if not ok and "NEED_M5" in str(status):
            return CommitResult(False, f"CCH 点播失败（M5：{status}）")
        # 观测优先：回执 TIMEOUT 但设备已起播 ⇒ 仍提交（真机主症状）
        confirmed = await obs.observe_play(timeout_s=3.0)
        if not confirmed and not ok:
            return CommitResult(False, f"CCH 点播失败（未观测到起播：{status}）")
        if not confirmed:
            logger.info("[PlaybackCommit] receipt ok but no fresh play observed; "
                        "committing on command acceptance", module="PlaybackCommit")
        # 唯一所有权提交点
        sr._cch_state.update({"entry": 0, "items": items, "no_advance": False,
                              "play_intent": intent, "played": []})
        # 呈现 + 投影：shield 保证调用方取消时后台任务仍继续（LLM tool 超时
        # 不再留下旧 UI/旧队列）；正常调用则等它完成再返回。
        presentation = asyncio.create_task(
            _present_and_project(play_url, item, items, meta, bg_url))
        await asyncio.shield(presentation)
        return CommitResult(True, "committed", presentation)
    finally:
        obs.end()


async def finalize_playback(
    *,
    play_url: str,
    item: dict,
    items: list[dict],
    meta: dict,
    bg_url: str,
    intent: str,
) -> CommitResult:
    """活实例轨收尾：设备已由 cast_native 起播，只做所有权提交 + 呈现。"""
    from core.tools import song_request as sr

    try:
        sr._cch_state.update({"entry": 0, "items": items, "no_advance": False,
                              "play_intent": intent, "played": []})
        presentation = asyncio.create_task(
            _present_and_project(play_url, item, items, meta, bg_url))
        return CommitResult(True, "committed", presentation)
    finally:
        from core.music import playback_observation as obs

        obs.end()


async def _present_and_project(play_url: str, item: dict, items: list[dict],
                               meta: dict, bg_url: str) -> None:
    """呈现层（曲名/封面 → 歌词 → 带歌词二次补投）+ 投影层。best-effort，不抛。"""
    from core.music import cch_cast, cch_history

    try:
        render_data = cch_cast.build_render_data(play_url, item, meta,
                                                 background_image=bg_url)
        await cch_cast.render_display(render_data)  # 先纯显示（不依赖歌词 15s）
        render_data = await cch_cast.attach_lyric(item, render_data)
        await cch_cast.render_display(render_data)  # 二次补投带歌词版本
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PlaybackCommit] presentation failed: {type(exc).__name__}",
                       module="PlaybackCommit")
    try:
        cch_history.note(items[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PlaybackCommit] history note failed: {type(exc).__name__}",
                       module="PlaybackCommit")
    try:
        await cch_cast.push_queue(items)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PlaybackCommit] queue push failed: {type(exc).__name__}",
                       module="PlaybackCommit")
