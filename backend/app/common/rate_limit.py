"""轻量内存滑动窗口速率限制器。

不依赖外部库，用 threading.Lock 保证线程安全。
每个 IP 维护一个请求时间戳列表，定期清理过期条目防止内存泄漏。
"""
from __future__ import annotations

import threading
import time
from functools import wraps
from typing import Callable

from flask import jsonify, request


class _SlidingWindowStore:
    """按 key 存储请求时间戳的滑动窗口。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, list[float]] = {}
        self._last_cleanup = time.monotonic()

    def is_allowed(self, key: str, max_requests: int, window_sec: int) -> bool:
        now = time.monotonic()
        with self._lock:
            # 每 300 秒清理一次过期条目，控制内存
            if now - self._last_cleanup > 300:
                self._gc(now)
                self._last_cleanup = now
            timestamps = self._store.get(key)
            if timestamps is None:
                timestamps = []
                self._store[key] = timestamps
            # 移除窗口外的旧时间戳
            cutoff = now - window_sec
            while timestamps and timestamps[0] <= cutoff:
                timestamps.pop(0)
            if len(timestamps) >= max_requests:
                return False
            timestamps.append(now)
            return True

    def _gc(self, now: float) -> None:
        """清理无时间戳记录的 key。调用方需持锁。"""
        stale = [k for k, v in self._store.items() if not v]
        for k in stale:
            del self._store[k]


# 全局共享存储（同一进程内所有限速规则共用一份数据，
# 但 login / register 使用不同的 key 前缀，互不干扰）
_store = _SlidingWindowStore()


def rate_limit(
    max_requests: int = 5,
    window_seconds: int = 60,
    key_prefix: str = "rl",
) -> Callable:
    """Flask 视图装饰器：按 IP 限速。

    - max_requests: 窗口内允许的最大请求数
    - window_seconds: 滑动窗口时长（秒）
    - key_prefix:   key 前缀（同进程内区分不同端点）

    超限返回 429 + Retry-After 头。
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = request.remote_addr or "127.0.0.1"
            key = f"{key_prefix}:{ip}"
            if not _store.is_allowed(key, max_requests, window_seconds):
                resp = jsonify({
                    "detail": "请求过于频繁，请稍后再试",
                    "retry_after_seconds": window_seconds,
                })
                resp.status_code = 429
                resp.headers["Retry-After"] = str(window_seconds)
                return resp
            return fn(*args, **kwargs)

        return wrapper

    return decorator
