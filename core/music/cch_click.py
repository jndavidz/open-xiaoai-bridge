"""CCH 历史点击事件 worker（票 23，design/21-23-device-module.md §2）。

数据流：:core cp_hook 命中云端 ContentPlay → 摘要 spool（设备文件，无 audioUrl）
→ tts_http :9102 `clickpoll`（读即删）→ 本 worker（1s 轮询）→ 时间窗去重
→ 复用语音点歌同一条链（`song_request.run` target=cch：早停 + 播报 + 取票 + M5 play），
不另写简化链（design §2.6：避免行为分叉）。

判别纪律（design §2.1）：native 无法区分「语音点歌的云端 ContentPlay」与「历史
点击」——同源。故 native 一律上报、本侧按时间窗去重；主判据 = 与最近一次主动
点播的归一化 name+artist 相同且在 `cch_click_dedupe_s`（默认 8s）内 → drop。
**不要**拿试听流 URL 特征当判据（两者同为试听流，无法区分）。

红线（design §7）：不 new MediaPlayer / 不 requestAudioFocus（票 16）；play 走
原厂内核；audioUrl/票据不进本模块日志；worker 自身起播也登记占窗（CP+createlist
同曲双发的实测形态，票 23 §2.71）。
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import deque

from core.utils.logger import logger

_POLL_S = 1.0            # design §2.6：1s 轮询
_SLOW_POLL_S = 5.0       # 连续失败降频
_FAIL_STREAK_SLOW = 3
_RECV_MAX = 10           # 单次 clickpoll 拉取上限（与 tts_http 默认一致）
_DEDUPE_CAP = 32         # design §2.6：deque 容量 32
_DEDUPE_DEFAULT_S = 8.0
_RESUME_AFTER_S = 1.5    # design §2.7：起播后 1.5s 读 state，paused 补一次 resume

# (ts, norm_name, norm_artist) 环形窗——每次主动点播（语音 + worker 自身）前登记
_recent: deque = deque(maxlen=_DEDUPE_CAP)

# 尾部括注（全/半角括号）：历史条目名常带「(四段)」「（Live）」等版本后缀
_BRACKET_RE = re.compile(r"[（(][^）)]*[）)]\s*$")


def _cfg(key, default=None):
    from core.tools import song_request as sr

    return sr._cfg(key, default)


def enabled() -> bool:
    """worker 总开关（cch_click_enabled，默认 False——模块 v0.4.9 刷机后由用户打开）。"""
    val = _cfg("cch_click_enabled", False)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def _norm(text) -> str:
    """归一化：去尾部括注 + 去全部空白 + 小写（design §2.6「归一化 name+artist」）。"""
    s = _BRACKET_RE.sub("", str(text or "")).strip().lower()
    return re.sub(r"\s+", "", s)


def note_recent_play(name: str, artist: str) -> None:
    """登记一次主动点播。song_request 的 CCH 播放路径与 worker 自身起播都会调——
    时间窗内的云端回声 ContentPlay 由 dedupe() 判重丢弃。"""
    nn = _norm(name)
    if nn:
        _recent.append((time.time(), nn, _norm(artist)))


def dedupe(name: str, artist: str, *, now: float | None = None) -> str:
    """返回 ""=放行；非空=drop 原因。窗宽 cch_click_dedupe_s（默认 8s）。

    artist 单侧缺失按 name 相似判（audioAnchor 与搜索结果的歌手字段常不同构）。
    """
    ts = time.time() if now is None else now
    try:
        window = float(_cfg("cch_click_dedupe_s", _DEDUPE_DEFAULT_S) or _DEDUPE_DEFAULT_S)
    except (TypeError, ValueError):
        window = _DEDUPE_DEFAULT_S
    nn, na = _norm(name), _norm(artist)
    if not nn:
        return ""
    for ets, en, ea in reversed(_recent):        # deque 按时间升序，倒序首个超窗即停
        if ts - ets > window:
            break
        if en == nn and (not ea or not na or ea == na):
            return f"dedupe({en})"
    return ""


async def _resume_if_paused() -> None:
    """云端随后下发 Pause（§2.71-3 实测 20:45:22.105）会打断刚起的播：1.5s 后读
    np STATE，观察到 paused 才补一次 resume（只补一次，防抖）。"""
    await asyncio.sleep(_RESUME_AFTER_S)
    try:
        from core.music import cch_cast, cch_np

        st = (cch_np.snapshot().get("state") or {})
        status = str((st.get("extras") or {}).get("statusStr") or "").lower()
        if status in ("pause", "paused"):
            await cch_cast.host_cmd("resume")
            logger.info("[CchClick] resume after cloud pause", module="CchClick")
    except Exception as exc:  # noqa: BLE001 - 恢复是尽力而为，不阻塞 worker
        logger.warning(f"[CchClick] resume check err: {type(exc).__name__}", module="CchClick")


async def _handle_event(ev: dict) -> None:
    """单事件：去重 → 占窗 → 复用语音点歌链（target=cch）。"""
    name = str(ev.get("name") or "")
    artist = str(ev.get("artist") or "")
    source = str(ev.get("source") or "")
    reason = dedupe(name, artist)
    if reason:
        logger.info(f"[CchClick] recv name={name} src={source} action=drop({reason})",
                    module="CchClick")
        return
    note_recent_play(name, artist)   # 自身占窗：CP+createlist 双发不二次起播
    logger.info(f"[CchClick] recv name={name} src={source} action=play", module="CchClick")
    from core.tools import song_request

    result = await song_request.run({"action": "play", "query": name,
                                     "artist": artist, "target": "cch"})
    logger.info(f"[CchClick] play done name={name} result={str(result)[:80]}",
                module="CchClick")
    asyncio.create_task(_resume_if_paused())


async def run_worker() -> None:
    """常驻轮询循环（APIServer.start 起任务；enabled()=False 不启动）。

    单条事件失败只计数不退出；连续 3 次拉取失败降频 5s（design §2.6）。
    """
    logger.info("[CchClick] worker started (poll=1s)", module="CchClick")
    fail_streak = 0
    delay = _POLL_S
    while True:
        try:
            from core.music import cch_cast

            ok, payload = await cch_cast.musicctl("clickpoll", {"max": _RECV_MAX})
            events = payload.get("events") if ok and isinstance(payload, dict) else None
            if events is None:
                raise RuntimeError("clickpoll not ok")
            fail_streak = 0
            delay = _POLL_S
            for ev in events:
                try:
                    await _handle_event(ev)
                except Exception as exc:  # noqa: BLE001 - 单条失败不影响 worker
                    logger.warning(f"[CchClick] handle err: {type(exc).__name__}",
                                   module="CchClick")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 拉取失败降频续跑
            fail_streak += 1
            if fail_streak >= _FAIL_STREAK_SLOW:
                delay = _SLOW_POLL_S
            logger.warning(f"[CchClick] poll err ({fail_streak}): {type(exc).__name__}",
                           module="CchClick")
        await asyncio.sleep(delay)
