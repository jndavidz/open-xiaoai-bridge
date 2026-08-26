"""内存环形日志缓冲。

给全局 logger 挂一个额外的 MemoryLogHandler，把最近的日志条目保存在
内存 deque 中（上限 LOG_BUFFER_SIZE，默认 2000 条），供后台面板增量
拉取（GET /api/admin/logs?after=<seq>）。不影响 stdout 输出，也不落盘。

seq 单调递增：客户端记录上次收到的最大 seq，下次携带 after=<seq> 只取
增量；缓冲溢出丢弃的旧条目数量通过 dropped 计数暴露，前端据此提示。
"""

import logging
import os
import threading
from collections import deque
from typing import Optional


def _buffer_size() -> int:
    try:
        return max(200, int(os.environ.get("LOG_BUFFER_SIZE", "2000")))
    except ValueError:
        return 2000


class MemoryLogHandler(logging.Handler):
    """线程安全的内存环形日志 Handler。"""

    def __init__(self, capacity: Optional[int] = None):
        super().__init__()
        self._capacity = capacity or _buffer_size()
        self._entries: deque[dict] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self.dropped = 0  # 因环形容量溢出而丢弃的累计条数

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            # 异常堆栈并入消息末尾（面板可直接看到 traceback）
            if record.exc_info and record.exc_info[0] is not None:
                message += "\n" + self.formatException(record.exc_info)
        except Exception:
            return

        entry = {
            "seq": 0,
            "ts": round(record.created, 3),
            # 用 levelno 反查纯净级别名：不依赖 record.levelname（可能被
            # 彩色 formatter 等前置 handler 附加工件）
            "level": logging.getLevelName(record.levelno),
            "msg": message,
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            if len(self._entries) == self._entries.maxlen:
                self.dropped += 1
            self._entries.append(entry)

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def get_after(self, after: int = -1, limit: int = 500) -> tuple[list[dict], int]:
        """返回 seq > after 的最多 limit 条日志与当前最大 seq。"""
        with self._lock:
            pending = [e for e in self._entries if e["seq"] > after]
            return pending[-limit:], self._seq


_memory_handler: Optional[MemoryLogHandler] = None
_attach_lock = threading.Lock()


def get_memory_log_handler() -> MemoryLogHandler:
    """获取（惰性创建）全局内存日志 Handler。"""
    global _memory_handler
    with _attach_lock:
        if _memory_handler is None:
            _memory_handler = MemoryLogHandler()
        return _memory_handler


def attach_memory_log_handler(target: logging.Logger) -> MemoryLogHandler:
    """把内存 Handler 挂到指定 logger（幂等）。"""
    handler = get_memory_log_handler()
    if handler not in target.handlers:
        target.addHandler(handler)
    return handler
