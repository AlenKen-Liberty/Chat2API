from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class AccountLock:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def try_acquire(self, account_id: str) -> bool:
        lock = self._locks[account_id]
        if lock.locked():
            return False
        await lock.acquire()
        return True

    def release(self, account_id: str) -> None:
        lock = self._locks[account_id]
        if lock.locked():
            lock.release()


class RateLimiter:
    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def check(self, account_id: str) -> bool:
        now = time.time()
        window = self._history[account_id]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= self.max_rpm:
            return False
        window.append(now)
        return True
