"""用量记账台账（P2 ticket 03）：每轮对话的数值化用量 → 每日 JSONL 落盘。

定位（2026-10-25 裁决 Q2「先记账后控制」）：

- **只记账，零控制行为**：本模块不做任何限额、断路、后端切换或降级判断；
  观测与查询是它仅有的两件事。限额/断路等控制面等 2 个月真实账单后再定。
- **只记数值**：请求次数、prompt/回复字符数、token（上游自报或字符估算）、
  TTS 字符、工具轮数、联网工具调用数、错误标志，以及 backend / model /
  session_key 三个聚合维度。不记任何对话文本（PII 纪律与 turn_trace 一致）。
- **数据源是 TurnTrace 扩展**：每轮结束时 turn_trace 把含新增用量字段的
  summary 交给 :func:`observe`，一条轮次记录 = 一行 JSON。
- **轻量持久化**：`data/usage/usage-YYYYMMDD.jsonl` 追加写（每轮一行），
  无数据库、无轮转服务；查询 = 读取近 N 天文件后内存聚合。
  环境变量 `USAGE_LEDGER_DIR` 覆盖目录（容器部署指向持久化卷）。
- **失败即静默**：记账的任何异常都不得影响对话、播报或面板。

开关：`usage_ledger.enabled`（config.py，默认 **False**——保守默认，开发/
测试环境不落盘；生产 deploy/config.py 置 True 先记账）或环境变量
`USAGE_LEDGER_ENABLED`（1/true/yes/on 开，0/false/no/off 关）；env > config。
开关只 gate **记录**；:func:`query` 不受开关影响（历史文件永远可查）。

token 估算口径（面板脚注同源）：上游响应带 `usage` 时记真实 token
（prompt_tokens/completion_tokens 或 input/output_tokens）；否则按
**1 token ≈ 1.6 字符**估算（中文为主的中英混合经验值，精度有限，
跑满后按真实账单校准——即 Q2 的两个月窗口）。真实值与估算值分开成列，
不做混合求和（部分请求报 usage 时混合求和会双算口径）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

ENV_USAGE_LEDGER_ENABLED = "USAGE_LEDGER_ENABLED"
ENV_USAGE_LEDGER_DIR = "USAGE_LEDGER_DIR"

# token 估算：字符 / 1.6 ≈ token（中文口径；详见模块 docstring）
CHARS_PER_TOKEN = 1.6

ESTIMATION_NOTE = (
    "token 为估算值：上游返回 usage 时用真实值（真实列），"
    "否则按 1 token ≈ 1.6 字符估算（估算列，中文口径）；"
    "两列口径独立不混加。本页仅记账展示，无任何限额/断路/切换控制。"
)

# 轮次记录的计数字段（聚合时求和；全部 int）
_COUNTER_FIELDS = (
    "turns",
    "requests",
    "prompt_chars",
    "completion_chars",
    "prompt_tokens",
    "completion_tokens",
    "tts_chars",
    "tool_rounds",
    "web_calls",
    "llm_errors",
    "llm_timeouts",
)

_WRITE_LOCK = threading.Lock()


def _flag(raw: Any) -> bool | None:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def _read_config_enabled() -> bool:
    """读 config.py 的 `usage_ledger.enabled`（不构造 ConfigManager，避免联网）。"""
    try:
        from core.utils.config_loader import ensure_config_module_loaded

        module = ensure_config_module_loaded()
        app_config = getattr(module, "APP_CONFIG", None)
        if not isinstance(app_config, dict):
            return False
        section = app_config.get("usage_ledger")
        if not isinstance(section, dict):
            return False
        return _flag(section.get("enabled")) is True
    except Exception:  # noqa: BLE001 - 配置不可读按关闭处理
        return False


def enabled(config_reader: Any = None) -> bool:
    """记账总开关：env 覆盖 config，默认 False（生产 deploy config 置 True）。"""
    override = _flag(os.environ.get(ENV_USAGE_LEDGER_ENABLED))
    if override is not None:
        return override
    reader = config_reader or _read_config_enabled
    try:
        return bool(reader())
    except Exception:  # noqa: BLE001 - 读配置失败按关闭处理
        return False


def ledger_dir() -> Path:
    env_dir = os.environ.get(ENV_USAGE_LEDGER_DIR, "").strip()
    if env_dir:
        return Path(env_dir)
    root = Path(__file__).resolve().parents[2]
    return root / "data" / "usage"


def _record_path(day: str, base_dir: Path | None = None) -> Path:
    return (base_dir or ledger_dir()) / f"usage-{day}.jsonl"


def estimate_tokens(chars: int) -> int:
    """字符 → token 估算（1 token ≈ 1.6 字符，向下取整非负）。"""
    if chars <= 0:
        return 0
    return int(chars / CHARS_PER_TOKEN)


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _str_or_empty(value: Any) -> str:
    return str(value) if value else ""


def observe(summary: dict[str, Any]) -> bool:
    """把一轮的 TurnTrace summary 写成一行 JSONL（任何异常静默吞掉）。

    返回是否成功落盘；开关关闭或 summary 非法时返回 False，不抛异常。
    """
    try:
        if not isinstance(summary, dict) or not enabled():
            return False
        local = time.localtime()
        record = {
            "ts": int(time.time()),
            "date": time.strftime("%Y-%m-%d", local),
            "turn": _int_or_zero(summary.get("turn")),
            "src": _str_or_empty(summary.get("src")),
            "backend": _str_or_empty(summary.get("backend")),
            "model": _str_or_empty(summary.get("model")),
            "session_key": _str_or_empty(summary.get("session_key")),
            "incomplete": _int_or_zero(summary.get("incomplete")),
            "requests": _int_or_zero(summary.get("llm_requests")),
            "prompt_chars": _int_or_zero(summary.get("prompt_chars")),
            "completion_chars": _int_or_zero(summary.get("llm_chars")),
            "prompt_tokens": _int_or_zero(summary.get("prompt_tokens")),
            "completion_tokens": _int_or_zero(summary.get("completion_tokens")),
            "tts_chars": _int_or_zero(summary.get("tts_chars")),
            "tool_rounds": _int_or_zero(summary.get("tool_rounds")),
            "web_calls": _int_or_zero(summary.get("web_calls")),
            "llm_errors": _int_or_zero(summary.get("llm_error")),
            "llm_timeouts": _int_or_zero(summary.get("llm_timeout")),
        }
        base = ledger_dir()
        base.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with _WRITE_LOCK:
            with open(_record_path(record["date"], base), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - 记账不得影响主流程
        return False


def _empty_row() -> dict[str, int]:
    return {field: 0 for field in _COUNTER_FIELDS}


def _add_into(row: dict[str, int], record: dict[str, Any]) -> None:
    row["turns"] += 1
    for field in _COUNTER_FIELDS[1:]:
        row[field] += _int_or_zero(record.get(field))


def _read_day(day: str, base_dir: Path) -> list[dict[str, Any]]:
    """读一天的记录；坏行（含并发追加的半行）跳过，不抛异常。"""
    path = _record_path(day, base_dir)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    records.append(item)
    except OSError:
        return []
    return records


def _date_range(days: int, today: str | None) -> list[str]:
    import datetime as _dt

    end = (
        _dt.date.fromisoformat(today)
        if today
        else _dt.date.fromtimestamp(time.time())
    )
    return [
        (end - _dt.timedelta(offset)).isoformat()
        for offset in range(days - 1, -1, -1)
    ]


def _finalize(row: dict[str, int]) -> dict[str, Any]:
    """补估算列（token 估算与真实值分列，不混加）。"""
    out = dict(row)
    out["prompt_tokens_est"] = estimate_tokens(row["prompt_chars"])
    out["completion_tokens_est"] = estimate_tokens(row["completion_chars"])
    return out


def _group_rows(
    records: list[dict[str, Any]], key_field: str
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, int]] = {}
    for record in records:
        key = _str_or_empty(record.get(key_field)) or "-"
        row = grouped.setdefault(key, _empty_row())
        _add_into(row, record)
    rows = [dict(_finalize(row), **{key_field: key}) for key, row in grouped.items()]
    rows.sort(key=lambda r: (-r["requests"], -r["turns"], r[key_field]))
    return rows


def query(
    days: int = 7, today: str | None = None, base_dir: Path | None = None
) -> dict[str, Any]:
    """聚合近 N 天用量：按天 / 按模型 / 按后端 / 按会话键。

    纯读文件 + 内存聚合；开关不影响查询。days 夹在 1..90。
    """
    days = max(1, min(90, int(days) if days is not None else 7))
    base = base_dir or ledger_dir()
    dates = _date_range(days, today)

    by_day: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    for day in dates:
        records = _read_day(day, base)
        all_records.extend(records)
        row = _empty_row()
        for record in records:
            _add_into(row, record)
        by_day.append(dict(_finalize(row), date=day))

    return {
        "days": days,
        "estimation_note": ESTIMATION_NOTE,
        "totals": _finalize(_aggregate(all_records)),
        "by_day": by_day,
        "by_model": _group_rows(all_records, "model"),
        "by_backend": _group_rows(all_records, "backend"),
        "by_session": _group_rows(all_records, "session_key"),
    }


def _aggregate(records: list[dict[str, Any]]) -> dict[str, int]:
    row = _empty_row()
    for record in records:
        _add_into(row, record)
    return row
