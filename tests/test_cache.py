import asyncio

import pytest

from met_bread.cache import TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_miss_then_hit():
    cache: TTLCache[str, int] = TTLCache(ttl_s=10, clock=FakeClock())
    assert cache.get("a") is None
    cache.set("a", 1)
    assert cache.get("a") == 1


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache: TTLCache[str, int] = TTLCache(ttl_s=10, clock=clock)
    cache.set("a", 1)
    clock.now = 9.9
    assert cache.get("a") == 1
    clock.now = 10.1
    assert cache.get("a") is None


async def test_get_or_compute_calls_factory_once_for_repeat_keys():
    cache: TTLCache[str, int] = TTLCache(ttl_s=10, clock=FakeClock())
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert await cache.get_or_compute("k", factory) == 42
    assert await cache.get_or_compute("k", factory) == 42
    assert calls == 1


async def test_concurrent_misses_share_one_computation():
    cache: TTLCache[str, int] = TTLCache(ttl_s=10, clock=FakeClock())
    calls = 0
    release = asyncio.Event()

    async def slow_factory() -> int:
        nonlocal calls
        calls += 1
        await release.wait()
        return 7

    tasks = [asyncio.create_task(cache.get_or_compute("k", slow_factory)) for _ in range(3)]
    await asyncio.sleep(0)  # let all three reach the miss path
    release.set()
    assert await asyncio.gather(*tasks) == [7, 7, 7]
    assert calls == 1


async def test_failed_computation_is_not_cached():
    cache: TTLCache[str, int] = TTLCache(ttl_s=10, clock=FakeClock())
    attempts = 0

    async def flaky() -> int:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return 1

    with pytest.raises(RuntimeError):
        await cache.get_or_compute("k", flaky)
    assert await cache.get_or_compute("k", flaky) == 1
    assert attempts == 2
