"""Orchestration: turn a search term into the N most recent matching works.

The obvious approach ("search, fetch every matching record, sort") does not work well against
the Met API: ``bread`` alone matches ~2 000 objects, each needing its own request, which takes
tens of seconds and trips the Met's bot protection. This module instead narrows the set of
records that must be fetched to a few dozen using only cheap, ID-only search requests:

1. Ask how many objects match at all.
2. Use the date-filtered search as a counting oracle to find the latest year ``Y`` such that
   at least ``limit`` matches *begin* in ``Y`` or later (``ranking.find_threshold_year``).
3. Work out which object IDs could have ``objectEndDate >= Y`` (``_candidate_ids``), fetch
   just those records, and sort them by their real ``objectEndDate``.

Step 3 has a subtlety worth understanding. The Met's date filter is a *containment* test
(``begin >= dateBegin AND end <= dateEnd``), so the window ``[Y, 9999]`` misses any object
that began before ``Y`` but ended after it, e.g. an object dated "1520 or 19th-20th century"
with ``objectEndDate == 1999``. The complement, however, *is* expressible: every object with
``end <= Y - 1`` is exactly the window ``[-9999, Y - 1]``. So

    candidates = all_matching_ids - ids_in([-9999, Y - 1])

is precisely the set with ``objectEndDate >= Y``, spanning objects included. This needs the
full ID list, which the Met only lets us page through up to 10 000 results; beyond that we
fall back to the window alone and flag the response as not ``exact``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from met_bread.cache import TTLCache
from met_bread.config import Settings
from met_bread.met_client import TooManyResultsError
from met_bread.models import MetObject, Work, WorksResponse
from met_bread.ranking import find_threshold_year, rank_works

logger = logging.getLogger(__name__)

#: The Met's own bounds for date filters (negative years are BC).
MIN_YEAR = -9999
MAX_YEAR = 9999

CacheKey = tuple[str, int]


class SearchClient(Protocol):
    """The slice of ``MetClient`` this service depends on; tests substitute an in-memory fake."""

    requests_made: int

    async def search_total(
        self, query: str, *, date_begin: int | None = None, date_end: int | None = None
    ) -> int:
        """Return the number of objects matching ``query`` (optionally within a date window)."""
        ...

    async def search_all_ids(
        self,
        query: str,
        *,
        date_begin: int | None = None,
        date_end: int | None = None,
        allow_truncation: bool = False,
    ) -> list[int]:
        """Return all matching object IDs, or raise ``TooManyResultsError`` past the cap."""
        ...

    async def get_object(self, object_id: int) -> MetObject | None:
        """Return the object record, or ``None`` if the Met does not know the ID."""
        ...


class WorksService:
    """Compute and cache the most recent works for a search term."""

    def __init__(
        self,
        client: SearchClient,
        settings: Settings,
        cache: TTLCache[CacheKey, WorksResponse] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._cache = cache if cache is not None else TTLCache(ttl_s=settings.cache_ttl_s)

    async def top_recent(self, query: str, limit: int) -> WorksResponse:
        """Return up to ``limit`` works matching ``query``, most recent ``objectEndDate`` first.

        Whitespace in ``query`` is normalised; the cache key is additionally case-folded because
        the Met's search is case-insensitive, so ``Bread`` and ``bread`` share one computation.
        """
        normalised = " ".join(query.split())
        if not normalised:
            raise ValueError("query must not be blank")
        if limit <= 0:
            raise ValueError("limit must be positive")

        key: CacheKey = (normalised.casefold(), limit)
        return await self._cache.get_or_compute(key, lambda: self._compute(normalised, limit))

    async def _compute(self, query: str, limit: int) -> WorksResponse:
        started = time.perf_counter()
        requests_before = self._client.requests_made

        total = await self._client.search_total(query)
        if total == 0:
            return WorksResponse(query=query, total_matches=0, exact=True, results=[])

        async def count_from(year: int) -> int:
            # "How many matches were made entirely in `year` or later?" — see module docstring.
            return await self._client.search_total(query, date_begin=year, date_end=MAX_YEAR)

        threshold = await find_threshold_year(count_from, need=limit)
        candidate_ids, exact = await self._candidate_ids(query, total, threshold)
        objects = await self._fetch_objects(candidate_ids)
        ranked = rank_works(objects, limit)

        logger.info(
            "query=%r total=%d threshold=%s candidates=%d exact=%s upstream_requests=%d "
            "elapsed_ms=%.0f",
            query,
            total,
            threshold,
            len(candidate_ids),
            exact,
            self._client.requests_made - requests_before,
            (time.perf_counter() - started) * 1000,
        )
        return WorksResponse(
            query=query,
            total_matches=total,
            exact=exact,
            results=[Work.from_met(obj) for obj in ranked],
        )

    async def _candidate_ids(
        self, query: str, total: int, threshold: int | None
    ) -> tuple[list[int], bool]:
        """Return ``(object IDs worth fetching, whether that set is provably complete)``.

        * No threshold means fewer than ``limit`` objects exist at all: fetch everything.
        * Within the Met's enumeration cap, use the complement trick for an exact answer.
        * Otherwise (or if the result set grows past the cap mid-request) fall back to the
          date window, which can miss objects that began before ``threshold``.
        """
        if threshold is None:
            ids = await self._client.search_all_ids(query, allow_truncation=True)
            return ids, total <= self._settings.max_enumerable

        if total <= self._settings.max_enumerable:
            try:
                return await self._complement_candidates(query, threshold)
            except TooManyResultsError:
                logger.warning("query=%r grew past the enumeration cap; using window", query)

        return await self._window_candidates(query, threshold)

    async def _complement_candidates(self, query: str, threshold: int) -> tuple[list[int], bool]:
        """Exact path: all matches minus those that ended before ``threshold``."""
        all_ids, before_ids = await asyncio.gather(
            self._client.search_all_ids(query),
            self._client.search_all_ids(query, date_begin=MIN_YEAR, date_end=threshold - 1),
        )
        candidates = sorted(set(all_ids) - set(before_ids))

        if len(candidates) > self._settings.max_candidates:
            # Pathological case: a very large number of objects with broad date ranges all
            # straddle the threshold. Fetching them all would be slow and hammer the Met, so
            # trade exactness for bounded latency and say so in the response.
            logger.warning(
                "query=%r has %d candidates (cap %d); using window",
                query,
                len(candidates),
                self._settings.max_candidates,
            )
            return await self._window_candidates(query, threshold)

        return candidates, True

    async def _window_candidates(self, query: str, threshold: int) -> tuple[list[int], bool]:
        """Approximate path: only objects that *begin* at or after ``threshold``."""
        ids = await self._client.search_all_ids(
            query, date_begin=threshold, date_end=MAX_YEAR, allow_truncation=True
        )
        return ids, False

    async def _fetch_objects(self, object_ids: list[int]) -> list[MetObject]:
        """Fetch records concurrently (the client bounds parallelism).

        IDs the search returned but the object endpoint does not know are skipped; the two
        indexes are not perfectly in sync. Any other failure aborts the whole request, because
        a partially fetched candidate set could silently produce a wrong top five.
        """
        results = await asyncio.gather(
            *(self._client.get_object(object_id) for object_id in object_ids),
            return_exceptions=True,
        )
        objects: list[MetObject] = []
        for object_id, result in zip(object_ids, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            if result is None:
                logger.info("object %d returned by search but not found; skipping", object_id)
                continue
            objects.append(result)
        return objects
