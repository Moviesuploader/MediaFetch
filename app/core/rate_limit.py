import asyncio
import time


class UserRateLimiter:
    def __init__(self, min_interval: float = 3.0) -> None:
        self.min_interval = min_interval
        self._last: dict[int, float] = {}
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        async with self._lock:
            previous = self._last.get(user_id, 0.0)
            if now - previous < self.min_interval:
                return False
            self._last[user_id] = now
            if len(self._last) > 10000:
                cutoff = now - max(self.min_interval * 10, 60)
                self._last = {
                    key: value
                    for key, value in self._last.items()
                    if value >= cutoff
                }
            return True
