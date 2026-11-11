"""RateLimiter tests with an injected clock and sleep, so nothing actually waits."""

from met_bread.pacing import RateLimiter


class Harness:
    """Frozen clock plus a recorder for requested sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


async def test_spaces_requests_one_interval_apart():
    h = Harness()
    limiter = RateLimiter(10, clock=h.clock, sleep=h.sleep)  # 10/s -> 0.1 s apart
    for _ in range(4):
        await limiter.acquire()
    # First call is immediate; each later call waits until its slot.
    assert h.sleeps == [0.1, 0.1, 0.1]


async def test_no_wait_when_enough_time_has_passed():
    h = Harness()
    limiter = RateLimiter(10, clock=h.clock, sleep=h.sleep)
    await limiter.acquire()
    h.now += 5.0
    await limiter.acquire()
    assert h.sleeps == []


async def test_zero_rate_disables_pacing():
    h = Harness()
    limiter = RateLimiter(0, clock=h.clock, sleep=h.sleep)
    for _ in range(50):
        await limiter.acquire()
    assert h.sleeps == []


async def test_burst_is_not_banked():
    # A long idle period must not grant a burst of back-to-back requests afterwards; that
    # is exactly what trips the Met's edge protection.
    h = Harness()
    limiter = RateLimiter(10, clock=h.clock, sleep=h.sleep)
    await limiter.acquire()
    h.now += 60.0
    await limiter.acquire()
    await limiter.acquire()
    assert h.sleeps == [0.1]
