"""Pure ranking logic: choosing a date threshold and ordering works.

Nothing in this module performs I/O. ``find_threshold_year`` is handed a coroutine that
answers "how many matches were made entirely in year Y or later?", which keeps the search
strategy unit-testable without touching the network.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Iterable, Sequence

from met_bread.models import MetObject

CountFrom = Callable[[int], Awaitable[int]]

#: First-round probe years, descending. 2100 is a sentinel above any real date so the
#: refinement loop always starts with a known upper bound; spacing widens into antiquity
#: where the collection is sparse; -9999 is the floor the Met itself uses.
DEFAULT_PROBES: tuple[int, ...] = (
    2100, 2000, 1900, 1800, 1700, 1600, 1500, 1400, 1300, 1200, 1100, 1000,
    500, 0, -1000, -3000, -9999,
)  # fmt: skip

#: How many evenly spaced years to probe per refinement round. Each round shrinks the
#: candidate span by this factor, so a 100-year span resolves to a single year in two rounds.
_PROBES_PER_ROUND = 10


async def find_threshold_year(
    count_from: CountFrom,
    *,
    need: int,
    probes: Sequence[int] = DEFAULT_PROBES,
) -> int | None:
    """Return the latest year ``Y`` such that at least ``need`` matches begin in ``Y`` or later.

    Why this exists: the Met search endpoint returns object IDs without dates and cannot sort,
    so the only way to learn an object's date is to fetch its full record. Fetching thousands
    of records per query is slow and trips the Met's bot protection. Instead we use the
    (cheap, ID-only) date-filtered search as a counting oracle to find a year ``Y`` that
    leaves only a handful of candidates with ``objectEndDate >= Y``.

    ``count_from(Y)`` must be monotonic non-increasing in ``Y`` (moving the threshold later
    can only shrink the set). That property is what makes the narrowing below valid.

    Strategy: one parallel round of coarse probes, then successive parallel rounds that split
    the remaining span into ten evenly spaced years, until the span is a single year. The
    whole search costs a few dozen tiny requests issued in ~4 round trips.

    Returns ``None`` when even the earliest probe yields fewer than ``need`` matches; the
    caller should then treat every match as a candidate.
    """
    if need <= 0:
        raise ValueError("need must be positive")
    if not probes or any(a <= b for a, b in itertools.pairwise(probes)):
        raise ValueError("probes must be a non-empty, strictly descending sequence")

    counts = await asyncio.gather(*(count_from(year) for year in probes))
    first_hit = next((i for i, count in enumerate(counts) if count >= need), None)
    if first_hit is None:
        return None

    # Invariant from here on: count_from(low) >= need and count_from(high) < need.
    low = probes[first_hit]
    high = probes[first_hit - 1] if first_hit > 0 else low + 100

    while high - low > 1:
        step = max(1, (high - low) // _PROBES_PER_ROUND)
        years = list(range(high - step, low, -step))  # strictly inside (low, high), descending
        counts = await asyncio.gather(*(count_from(year) for year in years))
        hit = next((i for i, count in enumerate(counts) if count >= need), None)
        if hit is None:
            # Every probed year had too few matches; the lowest probe is the new upper bound.
            high = years[-1]
        else:
            low = years[hit]
            high = years[hit - 1] if hit > 0 else high

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
