"""Process-wide request pacing for the Met API.

The Met documents an 80 requests/second ceiling, but its edge protection also reacts to
bursts. ``RateLimiter`` smooths our outgoing rate and deliberately does *not* bank unused
capacity: a client that was idle for a minute is not allowed a burst afterwards. (The Met's
longer-horizon request budget is handled separately by the cooldown in ``met_client.py``.)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """Allow at most ``rate_per_s`` calls per second, evenly spaced. ``0`` disables pacing.

    ``clock`` and ``sleep`` are injectable so tests can run without real waiting.
    """

    def __init__(
        self,
        rate_per_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_s < 0:
            raise ValueError("rate_per_s must be >= 0")
        self.rate_per_s = rate_per_s
        self._interval = 1.0 / rate_per_s if rate_per_s else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until the caller may issue a request."""
        if not self._interval:
            return
        async with self._lock:
            now = self._clock()
            # ``max`` is what prevents banking: an idle period never pulls the next slot
            # into the past, so the first request after idling is immediate but the second
            # still has to wait a full interval.
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
            wait = slot - now
        if wait > 0:
            await self._sleep(wait)
