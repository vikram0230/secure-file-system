"""In-process token-bucket rate limiter.

Limits are per API instance: with N instances a client can reach N x the
configured rate. That is acceptable for abuse dampening; a global limit needs
a shared store (see docs/DESIGN.md §12).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

MAX_TRACKED_KEYS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._refill_per_second = per_minute / 60.0
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= MAX_TRACKED_KEYS:
                self._evict_full(now)
            bucket = self._buckets[key] = _Bucket(tokens=self._capacity, updated=now)
        else:
            elapsed = now - bucket.updated
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_second)
            bucket.updated = now

        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def _evict_full(self, now: float) -> None:
        """Forget clients whose bucket has refilled; they behave identically to new ones."""
        refill_time = self._capacity / self._refill_per_second
        stale = [k for k, b in self._buckets.items() if now - b.updated >= refill_time]
        for key in stale:
            del self._buckets[key]
        if len(self._buckets) >= MAX_TRACKED_KEYS:
            self._buckets.clear()
