"""Pure ranking logic: choosing a date threshold and ordering works.

Nothing in this module performs I/O. ``find_threshold_year`` is handed a coroutine that
answers "how many matches have objectEndDate >= Y?", which keeps the search strategy
unit-testable without touching the network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from met_bread.models import MetObject

CountAtLeast = Callable[[int], Awaitable[int]]

#: The Met's own bounds for date filters (negative years are BC).
MIN_YEAR = -9999
MAX_YEAR = 9999


async def find_threshold_year(
    count_at_least: CountAtLeast,
    *,
    need: int,
    floor: int = MIN_YEAR,
    ceiling: int = MAX_YEAR,
) -> int | None:
    """Return the latest year ``Y`` such that at least ``need`` matches end in ``Y`` or later.

    Why this exists: the Met search endpoint returns object IDs without dates and cannot sort,
    so the only way to learn an object's date is to fetch its full record. Fetching thousands
    of records per query is slow and quickly exhausts the Met's request budget. Instead we use
    the cheap, ID-only, date-filtered search as a counting oracle to find a year ``Y`` that
    leaves only a handful of candidates with ``objectEndDate >= Y``.

    ``count_at_least(Y)`` must be monotonic non-increasing in ``Y`` (moving the threshold
    later can only shrink the set). That property is what makes bisection valid.

    Sequential bisection is used rather than parallel probing because outgoing requests are
    paced anyway, so parallelism buys nothing while costing extra requests: about 15 probes
    cover the Met's whole date range.

    Returns ``None`` when fewer than ``need`` matches exist at the floor; the caller should
    then treat every match as a candidate.
    """
    if need <= 0:
        raise ValueError("need must be positive")
    if floor > ceiling:
        raise ValueError("floor must not exceed ceiling")

    if await count_at_least(floor) < need:
        return None

    # Invariant: count_at_least(low) >= need, and count_at_least(high) < need
    # (``high`` starts one past the ceiling, where no object can end).
    low, high = floor, ceiling + 1
    while high - low > 1:
        mid = (low + high) // 2
        if await count_at_least(mid) >= need:
            low = mid
        else:
            high = mid
    return low


def rank_works(objects: Iterable[MetObject], limit: int) -> list[MetObject]:
    """Order works most-recent-first by ``object_end_date`` and keep the top ``limit``.

    Ties are broken by later ``object_begin_date`` (a narrower, more recent range wins), then
    by ascending object ID so the output is deterministic across runs.
    """
    ordered = sorted(
        objects,
        key=lambda obj: (-obj.object_end_date, -obj.object_begin_date, obj.object_id),
    )
    return ordered[:limit]
