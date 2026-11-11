"""Thin asynchronous client for the Met Collection API.

Responsibilities, and nothing more:

* Build correct URLs/params for ``/v1.1/search`` and ``/v1/objects/{id}``.
* Enforce politeness: an identifying User-Agent, bounded concurrency, and retries with
  exponential backoff for transient failures (including the bot-protection HTML page the
  Met's edge serves with a 403 when requests arrive in a burst).
* Page through search results while respecting the Met's hard 10 000-result ceiling.

Everything that interprets the data (thresholds, ranking) lives elsewhere.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any

import httpx

from met_bread.config import Settings
from met_bread.models import MetObject, SearchResponse

logger = logging.getLogger(__name__)

#: Statuses worth retrying. 403 is included deliberately: the Met returns it from its
#: bot-protection layer under bursty load, and it clears within seconds.
_RETRYABLE_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})


class MetUpstreamError(Exception):
    """The Met API could not be reached, or kept failing after all retries."""


class TooManyResultsError(Exception):
    """A search matched more objects than the Met API allows us to enumerate."""

    def __init__(self, total: int, cap: int) -> None:
        super().__init__(f"search matched {total} objects; only {cap} can be enumerated")
        self.total = total
        self.cap = cap


class MetClient:
    """Async client over an injected ``httpx.AsyncClient``.

    The HTTP client is injected (rather than created here) so the application can share one
    connection pool and tests can point it at a mock transport.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        #: Total HTTP attempts issued, including retries. Useful for logging and tests.
        self.requests_made = 0

    # -- Search -----------------------------------------------------------------------------

    async def search_ids(
        self,
        query: str,
        *,
        date_begin: int | None = None,
        date_end: int | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> SearchResponse:
        """Run one page of ``/v1.1/search``.

        ``date_begin``/``date_end`` form a *containment* filter on the Met side: an object is
        included only if its ``objectBeginDate >= date_begin`` **and**
        ``objectEndDate <= date_end``. They must be supplied together.
        """
        params: dict[str, Any] = {
            "q": query,
            "offset": offset,
            "limit": limit if limit is not None else self._settings.page_size,
        }
        params.update(_date_params(date_begin, date_end))
        data = await self._get_json(f"{self._base_url}/v1.1/search", params=params)
        return SearchResponse.model_validate(data)

    async def search_total(
        self,
        query: str,
        *,
        date_begin: int | None = None,
        date_end: int | None = None,
    ) -> int:
        """Return only the match count for a search, using the smallest possible page."""
        page = await self.search_ids(query, date_begin=date_begin, date_end=date_end, limit=1)
        return page.total

    async def search_all_ids(
        self,
        query: str,
        *,
        date_begin: int | None = None,
        date_end: int | None = None,
        allow_truncation: bool = False,
    ) -> list[int]:
        """Return every matching object ID, sorted ascending.

        The Met refuses ``offset + limit > 10 000``, so result sets larger than that cannot be
        fully enumerated. By default that raises ``TooManyResultsError`` so callers can choose
        a fallback; with ``allow_truncation=True`` the first 10 000 reachable IDs are returned.
        """
        first = await self.search_ids(query, date_begin=date_begin, date_end=date_end)
        cap = self._settings.max_enumerable
        if first.total > cap and not allow_truncation:
            raise TooManyResultsError(first.total, cap)

        page_size = self._settings.page_size
        reachable = min(first.total, cap)
        remaining_offsets = range(page_size, reachable, page_size)
        pages = await asyncio.gather(
            *(
                self.search_ids(query, date_begin=date_begin, date_end=date_end, offset=offset)
                for offset in remaining_offsets
            )
        )

        # A set, not a list: the index can shift between page requests, so defensively
        # de-duplicate rather than trust page boundaries to be disjoint.
        ids = set(first.object_ids)
        for page in pages:
            ids.update(page.object_ids)
        return sorted(ids)

    # -- Objects ----------------------------------------------------------------------------

    async def get_object(self, object_id: int) -> MetObject | None:
        """Fetch one object record, or ``None`` if the Met reports it does not exist."""
        data = await self._get_json(
            f"{self._base_url}/v1/objects/{object_id}", params=None, none_on_404=True
        )
        return None if data is None else MetObject.model_validate(data)

    # -- Transport --------------------------------------------------------------------------

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        none_on_404: bool = False,
    ) -> Any:
        """GET ``url`` and return its decoded JSON body, retrying transient failures.

        Retryable: transport errors, the statuses in ``_RETRYABLE_STATUSES``, and any
        response whose body is not JSON (the bot-protection interstitial is HTML even when
        it arrives with a 200). Not retryable: 404 (optionally mapped to ``None``) and other
        4xx responses, which indicate a bug on our side rather than a transient upstream issue.
        """
        attempts = self._settings.max_retries + 1
        failure = "unknown"

        for attempt in range(1, attempts + 1):
            if attempt > 1:
                await asyncio.sleep(self._backoff_seconds(attempt))

            self.requests_made += 1
            try:
                # Only the request itself is under the semaphore; backoff sleeps happen
                # outside it so a stalled retry does not hold a slot other calls could use.
                async with self._semaphore:
                    response = await self._http.get(
                        url,
                        params=params,
                        headers={"User-Agent": self._settings.user_agent},
                        timeout=self._settings.request_timeout_s,
                    )
            except httpx.TransportError as exc:
                failure = f"transport error ({type(exc).__name__})"
                logger.warning("Met request failed: %s url=%s attempt=%d", failure, url, attempt)
                continue

            status = response.status_code
            if status == 404 and none_on_404:
                return None
            if status in _RETRYABLE_STATUSES or status >= 500:
                failure = f"HTTP {status}"
                logger.warning("Met request failed: %s url=%s attempt=%d", failure, url, attempt)
                continue
            if status != 200:
                raise MetUpstreamError(f"unexpected HTTP {status} from {url}")

            try:
                return response.json()
            except ValueError:
                # Never log the body: the interstitial is a large HTML page.
                failure = "non-JSON body"
                logger.warning("Met request failed: %s url=%s attempt=%d", failure, url, attempt)
                continue

        raise MetUpstreamError(
            f"Met API request failed after {attempts} attempts ({failure}): {url}"
        )

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with full jitter: base * 2^(n-1) plus up to one extra base."""
        base = self._settings.backoff_base_s
        return base * math.pow(2, attempt - 2) + random.uniform(0, base)


def _date_params(date_begin: int | None, date_end: int | None) -> dict[str, int]:
    """Validate and build the Met's date filter parameters.

    The Met silently ignores ``dateBegin`` without ``dateEnd`` (returning the unfiltered set),
    so a half-specified filter is treated as a programming error rather than passed through.
    """
    if (date_begin is None) != (date_end is None):
        raise ValueError("date_begin and date_end must be provided together")
    if date_begin is None or date_end is None:
        return {}
    return {"dateBegin": date_begin, "dateEnd": date_end}
