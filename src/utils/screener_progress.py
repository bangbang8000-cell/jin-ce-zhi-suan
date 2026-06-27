"""AI 筛选进度日志的线程安全缓冲。

后端关键步骤往这里 push 日志，前端通过 SSE 端点实时拉取。
每个客户端按自己的 offset 读取，互不干扰。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class _ProgressEntry:
    level: str  # info / warning / success / error
    message: str
    ts: float


class ScreenerProgressLog:
    def __init__(self, max_entries: int = 500):
        self._lock = threading.Lock()
        self._entries: List[_ProgressEntry] = []
        self._max = max_entries

    def push(self, message: str, level: str = "info"):
        with self._lock:
            self._entries.append(_ProgressEntry(level=level, message=message, ts=time.time()))
            if len(self._entries) > self._max:
                self._entries = self._entries[-self._max:]

    def read_since(self, offset: int) -> tuple:
        """返回 (offset, [新条目...])，条目为 dict。"""
        with self._lock:
            new_entries = self._entries[offset:]
            new_offset = offset + len(new_entries)
            items = [
                {"level": e.level, "message": e.message, "ts": e.ts}
                for e in new_entries
            ]
            return new_offset, items

    def clear(self):
        with self._lock:
            self._entries.clear()


# 全局单例，所有请求共享同一个缓冲
progress_log = ScreenerProgressLog()
