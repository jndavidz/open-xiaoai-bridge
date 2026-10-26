"""对话记忆 JSONL 存储（spec topic-switch-and-memory 票 04）。

定位：bridge 是上下文唯一权威——每轮对话追加写入 NAS 本地 JSONL，
aurora 会话 TTL 过期/换话题导致的上下文断档由 bridge 在建请求时
自行决定是否注入最近 N 轮摘要（纯文本截取，无 LLM 摘要）。

隐私边界：记忆只落 bridge 数据卷（NAS 本地），注入片段随常规请求走，
不整份外发；目录不入 git（.gitignore 排除 data/memory/）。

文件形状：<dir>/<session_key>.<YYYYMMDD>.jsonl，按天滚动；每行
{"ts": ISO8601, "session_key": str, "user": str, "assistant": str}。
损坏行跳过不抛（用户数据，单条脏不影响整体读取）。
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from core.utils.logger import logger

# session_key 形如 agent:<agentId>:<rest>——文件名安全化（保留字母数字-_，其余换 _）
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

# 注入摘要的护栏上限：单轮文本截断（防超长回复把 system 撑爆）
_SUMMARY_TURN_MAX_CHARS = 300
_SUMMARY_MAX_TURNS_CAP = 20


def _safe_session_name(session_key: str) -> str:
    return _SAFE_NAME_RE.sub("_", session_key or "default")


def day_path(directory: Path, session_key: str, day: datetime) -> Path:
    """某天的记忆文件路径：<dir>/<safe_session>.<YYYYMMDD>.jsonl。"""
    name = f"{_safe_session_name(session_key)}.{day.strftime('%Y%m%d')}.jsonl"
    return Path(directory) / name


def append_turn(
    directory: Path,
    session_key: str,
    user: str,
    assistant: str,
    now: datetime,
) -> bool:
    """追加一轮对话；目录不存在则创建。失败只记日志返回 False（记忆不阻断对话）。"""
    try:
        path = day_path(directory, session_key, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": now.isoformat(timespec="seconds"),
            "session_key": session_key,
            "user": str(user or ""),
            "assistant": str(assistant or ""),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 - 记忆写入绝不阻断对话
        logger.warning(f"[Memory] append failed: {type(exc).__name__}: {exc}")
        return False


def purge_expired(directory: Path, retain_days: int, now: datetime) -> int:
    """惰性清理超过保留天数的记忆文件，返回删除数（目录不存在返回 0）。"""
    if retain_days <= 0:
        return 0
    cutoff = (now - timedelta(days=retain_days)).date()
    removed = 0
    try:
        root = Path(directory)
        if not root.is_dir():
            return 0
        for path in root.glob("*.jsonl"):
            match = re.search(r"\.(\d{8})\.jsonl$", path.name)
            if not match:
                continue
            try:
                file_day = datetime.strptime(match.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if file_day < cutoff:
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning(f"[Memory] purge failed for {path.name}: {exc}")
        return removed
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断对话
        logger.warning(f"[Memory] purge scan failed: {type(exc).__name__}: {exc}")
        return removed


def _read_file_turns(path: Path, session_key: str) -> list[dict]:
    turns: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 损坏行跳过
                if not isinstance(record, dict):
                    continue
                if record.get("session_key") != session_key:
                    continue
                turns.append(record)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 - 读取失败降级为空记忆
        logger.warning(f"[Memory] read failed for {path.name}: {type(exc).__name__}: {exc}")
        return []
    return turns


def recent_turns(directory: Path, session_key: str, limit: int, now: datetime) -> list[dict]:
    """跨日合并取最近 limit 轮（按 ts 升序返回：旧 → 新）。"""
    if limit <= 0:
        return []
    root = Path(directory)
    if not root.is_dir():
        return []
    limit = min(limit, _SUMMARY_MAX_TURNS_CAP)
    files: list[Path] = []
    for path in root.glob("*.jsonl"):
        match = re.search(r"\.(\d{8})\.jsonl$", path.name)
        if not match or not path.name.startswith(_safe_session_name(session_key) + "."):
            continue
        files.append(path)
    # 文件名含日期：倒序读新文件，凑够 limit 即止（ts 最终排序兜底）
    files.sort(key=lambda p: p.name, reverse=True)
    collected: list[dict] = []
    for path in files:
        collected = _read_file_turns(path, session_key) + collected
        if len(collected) >= limit:
            break
    collected.sort(key=lambda r: str(r.get("ts", "")))
    return collected[-limit:]


def summary_block(turns: list[dict]) -> str:
    """把最近 N 轮拼成注入前缀（system 消息内容）。空轮次返回空串。"""
    if not turns:
        return ""
    lines = ["此前对话摘要（供参考，非当前话题）："]
    for record in turns:
        user = str(record.get("user", ""))[:_SUMMARY_TURN_MAX_CHARS]
        assistant = str(record.get("assistant", ""))[:_SUMMARY_TURN_MAX_CHARS]
        ts = str(record.get("ts", ""))[:16].replace("T", " ")
        lines.append(f"- {ts} 用户：{user}｜助手：{assistant}")
    return "\n".join(lines)
