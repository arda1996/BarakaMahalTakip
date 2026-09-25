"""Endpoint basina kayan pencere limiti.

Trendyol Go: ayni endpoint'e 10 saniyede en fazla 50 istek, sonrasi 429.
"""

import asyncio
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self, max_calls: int, per_seconds: float) -> None:
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, key: str) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                hits = self._hits[key]
                while hits and now - hits[0] > self.per_seconds:
                    hits.popleft()
                if len(hits) < self.max_calls:
                    hits.append(now)
                    return
                wait_for = self.per_seconds - (now - hits[0])
            await asyncio.sleep(max(wait_for, 0.01))
