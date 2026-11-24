"""CCH 收藏即时同步（2026-11-24）：长轮询 Songloft 收藏变更 → 全量写设备 favorites.json。

背景：gadget 只在 V2 响应到达时替换收藏 tab，且读设备 favorites.json 静态文件；
手机 Songloft App 改收藏后若无进程同步该文件，gadget 一直显示旧内容。本 worker 用
Songloft upstream 的 `GET /playlists/favorites/changes` 长轮询（25s 事件驱动）感知变化，
变化即调现成的 `cch_cast.push_favorites()` 全量下发，用户下次进收藏 tab 即见最新数据。

真相源 = Songloft 服务端；本 worker 只做「变更通知 → 镜像到设备文件」。
复用已 VERIFIED 的链路：songloft_api（token/API） + cch_cast.push_favorites（9102 favfile）。
"""
from __future__ import annotations

import asyncio

from core.utils.logger import logger

_POLL_S = 1.0            # 长轮询返回后立即发起下一次（端点自身挂起 25s）
_SLOW_POLL_S = 10.0      # 连续失败降频（避免 Songloft 未部署端点时刷屏）
_FAIL_STREAK_SLOW = 3


def _cfg(key: str, default=None):
    from core.tools import song_request as sr

    return sr._cfg(key, default)


def enabled() -> bool:
    """worker 总开关（cch_fav_sync_enabled，默认 True——纯 bridge 侧，不依赖设备刷机；
    生产 Songloft 未部署新端点时 404 失败会降频到 10s，不影响其它链路）。"""
    val = _cfg("cch_fav_sync_enabled", True)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def apply_change_response(prev_cursor: str | None, response: object) -> tuple[str | None, bool]:
    """长轮询响应 → (new_cursor, changed)。纯函数，规约见 tests/test_cch_fav_sync.py。

    异常响应（非 dict / 缺 cursor）→ 回到 None（下次触发首同步，宁可多写），changed=False。
    """
    if not isinstance(response, dict):
        return None, False
    cursor = response.get("cursor")
    changed = response.get("changed")
    # 两个字段都必须存在且类型正确，否则视为 malformed → 回到 None 触发全量重同步（fail-closed）
    if not isinstance(cursor, str) or not cursor or not isinstance(changed, bool):
        return None, False
    return cursor, changed


def should_push(prev_cursor: str | None, new_cursor: str | None, changed: bool) -> bool:
    """首次 / changed=true / cursor 变化 → push；否则不写设备文件。"""
    if prev_cursor is None:
        return True
    return changed or new_cursor != prev_cursor


async def run_worker() -> None:
    """常驻长轮询循环（APIServer.start 起任务）。失败只降频续跑，不退出。"""
    logger.info("[CchFavSync] worker started (long-poll favorites/changes)", module="CchFavSync")
    cursor: str | None = None
    fail_streak = 0
    delay = _POLL_S
    try:
        while True:
            try:
                import aiohttp

                from core.music import cch_cast, songloft_api

                async with aiohttp.ClientSession() as http:
                    response = await songloft_api.favorites_changes(cursor, http=http)
                new_cursor, changed = apply_change_response(cursor, response)
                if should_push(cursor, new_cursor, changed):
                    ok, payload = await cch_cast.push_favorites()
                    if not ok:
                        raise RuntimeError(f"push_favorites failed: {payload}")
                    logger.info("[CchFavSync] favorites changed → pushed to device",
                                module="CchFavSync")
                cursor = new_cursor
                fail_streak = 0
                delay = _POLL_S
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败降频续跑
                fail_streak += 1
                if fail_streak >= _FAIL_STREAK_SLOW:
                    delay = _SLOW_POLL_S
                # 失败时清空 cursor，恢复后立即做一次全量写（防止失败窗口内的变更漏同步）
                cursor = None
                # token 被撤销/失效 → 清缓存，下一轮重新登录（否则 401 持续到 6.5 天自然过期）
                if "http 401" in str(exc):
                    from core.music import song_bridge

                    song_bridge.invalidate_songloft_token()
                # 保留异常正文：生产未部署端点时区分 http 404 与真故障/鉴权失败
                logger.warning(f"[CchFavSync] poll err ({fail_streak}): "
                               f"{type(exc).__name__}: {exc}",
                               module="CchFavSync")
            await asyncio.sleep(delay)
    finally:
        logger.info("[CchFavSync] worker stopped", module="CchFavSync")
