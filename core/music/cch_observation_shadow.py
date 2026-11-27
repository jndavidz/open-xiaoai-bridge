"""CCH 观测影子（ticket 04 / P3）：只记录不控制。

复用影子 KWS（`core/services/audio/kws/shadow.py`）的既有形态：
默认关闭、旁路调用、异常只吞不影响主链、按天 JSONL + context 留证、
`snapshot()` 只读诊断。它不是第二套判定——新鲜度门仍在
`playback_observation.observe_play`（唯一真相源），本模块只补上
「bridge 下发 cmd_id ↔ 设备 STATE 上报」的关联留证（现状空白），
供后续 focus/观测接入策略面前做可靠性评估。

不变量：
1. 默认关闭（`enabled()` 读 `cch_shadow.enabled` 配置，缺省 False）。
2. `record_*` 任何异常都吞掉，绝不打断调用方（命令下发 / STATE 编排）。
3. 只写 JSONL，不写 `_np_state`、不触发换代/恢复/控制。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.utils.logger import logger


class CchObservationShadow:
    def __init__(self, log_dir: Path | None = None):
        self._log_dir = log_dir

    def enabled(self) -> bool:
        """默认关；显式配 `cch_shadow.enabled=true` 才记录。"""
        try:
            from core.tools.song_request import _cfg
            from core.music.cch_config import cfg_bool

            return cfg_bool(_cfg("cch_shadow.enabled", False), False)
        except Exception:  # noqa: BLE001 - 配置读不到按关闭处理
            return False

    def _path(self) -> Path:
        d = self._log_dir
        if d is None:
            d = Path("data/shadow")
        d.mkdir(parents=True, exist_ok=True)
        return d / f"cch-observation-{time.strftime('%Y%m%d')}.jsonl"

    def _write(self, record: dict) -> None:
        with open(self._path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def record_dispatch(self, cmd: str, cmd_id: str, *, entry=None,
                        epoch=None, url: str = "") -> None:
        """命令下发留证（旁路）。url 只记路径摘要，不记完整票据/凭据。"""
        if not self.enabled():
            return
        try:
            self._write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "kind": "dispatch", "cmd": cmd, "cmd_id": cmd_id,
                         "entry": entry, "epoch": epoch,
                         "url_probe": url.split("?", 1)[0][-80:] if url else ""})
        except Exception as exc:  # noqa: BLE001 - 影子失败不影响主链
            logger.debug(f"[CchShadow] dispatch record failed: {type(exc).__name__}",
                         module="CchShadow")

    def record_state(self, kind: str, extras: dict, device_ts: str,
                     observed_at: float) -> None:
        """设备 STATE 上报留证（旁路），与 dispatch.cmd_id 按时间邻近关联。"""
        if not self.enabled():
            return
        try:
            self._write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "kind": "state", "event": kind, "extras": extras or {},
                         "device_ts": device_ts, "observed_at": observed_at})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CchShadow] state record failed: {type(exc).__name__}",
                         module="CchShadow")

    def record_position(self, status: str, progress_ms, device_ts: str,
                        observed_at: float) -> None:
        """设备进度观测（ticket 任务2）：只记录，不参与任何控制判定。

        worker 已在 STATE extras 上报 progress（毫秒字符串），bridge 此前
        只透传不消费；本方法把它按 `kind=position` 留证，供先观察后接入。
        """
        if not self.enabled():
            return
        try:
            try:
                progress = int(progress_ms)
            except (TypeError, ValueError):
                progress = -1  # 非法进度留证但不猜值
            self._write({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "kind": "position", "status": status,
                         "progress_ms": progress, "device_ts": device_ts,
                         "observed_at": observed_at,
                         "policy": "position-only-no-control"})
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[CchShadow] position record failed: {type(exc).__name__}",
                         module="CchShadow")

    def snapshot(self) -> dict:
        """只读诊断（计数），不暴露票据/凭据。"""
        try:
            files = list(self._path().parent.glob("cch-observation-*.jsonl"))
            return {"enabled": self.enabled(), "files": len(files)}
        except Exception:  # noqa: BLE001
            return {"enabled": False, "files": 0}


_SHADOW = CchObservationShadow()


def record_dispatch(cmd: str, cmd_id: str, *, entry=None, epoch=None, url="") -> None:
    _SHADOW.record_dispatch(cmd, cmd_id, entry=entry, epoch=epoch, url=url)


def record_state(kind: str, extras: dict, device_ts: str, observed_at: float) -> None:
    _SHADOW.record_state(kind, extras, device_ts, observed_at)


def record_position(status: str, progress_ms, device_ts: str, observed_at: float) -> None:
    _SHADOW.record_position(status, progress_ms, device_ts, observed_at)


def snapshot() -> dict:
    return _SHADOW.snapshot()
