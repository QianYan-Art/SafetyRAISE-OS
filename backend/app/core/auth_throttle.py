from __future__ import annotations

import math
import time
from collections import deque
from contextlib import contextmanager
from threading import Lock, Semaphore
from typing import Iterator

from app.core.exceptions import RateLimitExceededError


class AuthRateLimiter:
    """按 IP 与账号维度执行滑动窗口认证限流，防止暴力破解与 CPU 耗尽。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check_and_record(
        self,
        *,
        bucket_key: str,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            return
        ts = time.monotonic() if now is None else now
        cutoff = ts - window_seconds
        with self._lock:
            queue = self._buckets.get(bucket_key)
            if queue is None:
                queue = deque()
                self._buckets[bucket_key] = queue
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if len(queue) >= limit:
                retry_after = max(1, int(math.ceil(queue[0] + window_seconds - ts)))
                raise RateLimitExceededError(
                    "认证请求过于频繁，请稍后重试。",
                    retry_after=retry_after,
                )
            queue.append(ts)
            if len(self._buckets) > 4096:
                self._prune_locked(cutoff)

    def clear_bucket(self, bucket_key: str) -> None:
        with self._lock:
            self._buckets.pop(bucket_key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _prune_locked(self, cutoff: float) -> None:
        expired_keys = [
            key for key, queue in self._buckets.items() if not queue or queue[-1] <= cutoff
        ]
        for key in expired_keys:
            self._buckets.pop(key, None)


_GLOBAL_AUTH_LIMITER = AuthRateLimiter()
_HASH_SEMAPHORE_LOCK = Lock()
_HASH_SEMAPHORE_LIMIT = 4
_HASH_SEMAPHORE = Semaphore(_HASH_SEMAPHORE_LIMIT)


def get_auth_rate_limiter() -> AuthRateLimiter:
    return _GLOBAL_AUTH_LIMITER


def reset_auth_rate_limiter() -> None:
    _GLOBAL_AUTH_LIMITER.reset()


@contextmanager
def bounded_password_hash_slot(*, max_concurrency: int = 4, acquire_timeout: float = 5.0) -> Iterator[None]:
    """限制并发 PBKDF2 计算槽位，避免认证洪泛耗尽 Web Worker 线程池。"""
    global _HASH_SEMAPHORE, _HASH_SEMAPHORE_LIMIT
    effective_limit = max(1, int(max_concurrency))
    with _HASH_SEMAPHORE_LOCK:
        if effective_limit != _HASH_SEMAPHORE_LIMIT:
            _HASH_SEMAPHORE_LIMIT = effective_limit
            _HASH_SEMAPHORE = Semaphore(effective_limit)
        semaphore = _HASH_SEMAPHORE

    acquired = semaphore.acquire(timeout=acquire_timeout)
    if not acquired:
        raise RateLimitExceededError(
            "当前认证服务繁忙，请稍后重试。",
            retry_after=2,
        )
    try:
        yield
    finally:
        semaphore.release()
