"""A small in-process TTL cache with request coalescing.

The Met's collection changes slowly, so caching a query's result for an hour is a huge win:
a cold query costs a few dozen upstream requests, a warm one costs none. Coalescing ensures
that concurrent requests for the same key share a single computation instead of each fanning
out to the Met.

This is intentionally minimal (no eviction beyond expiry, no size cap) because the key space
is tiny: distinct (query, limit) pairs. Swap for Redis if the service is ever scaled out.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TTLCache[K, V]:
    """Map keys to values that expire ``ttl_s`` seconds after being set.

    ``clock`` is injectable so tests can control time; it defaults to ``time.monotonic``.
    A ``ttl_s`` of 0 effectively disables caching. ``None`` is not a storable value.
    """

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: dict[K, tuple[float, V]] = {}
        self._inflight: dict[K, asyncio.Task[V]] = {}

    def get(self, key: K) -> V | None:
        """Return the cached value, or ``None`` if absent or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return value

    def set(self, key: K, value: V) -> None:
        """Store ``value`` under ``key`` for the configured TTL."""
        self._entries[key] = (self._clock() + self._ttl_s, value)

    async def get_or_compute(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        """Return the cached value for ``key``, computing and storing it on a miss.

        Concurrent callers that miss on the same key await one shared computation.
        A failed computation is not cached and its exception propagates to every waiter.
        """
        cached = self.get(key)
        if cached is not None:
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._compute(key, factory))
            self._inflight[key] = task

        # ``shield`` matters: awaiting a Task directly would cancel it if *this* caller were
        # cancelled (e.g. client disconnect), which would also fail every other waiter.
        return await asyncio.shield(task)

    async def _compute(self, key: K, factory: Callable[[], Awaitable[V]]) -> V:
        try:
            value = await factory()
            self.set(key, value)
            return value
        finally:
            self._inflight.pop(key, None)
