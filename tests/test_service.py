"""WorksService tests against an in-memory fake that reproduces the Met's search semantics."""

from __future__ import annotations

import pytest

from met_bread.config import Settings
from met_bread.met_client import TooManyResultsError
from met_bread.models import MetObject
from met_bread.service import WorksService


class FakeMetClient:
    """Emulates the Met: ID-only search with a *containment* date filter, full object fetch.

    ``objects`` is a list of ``(object_id, begin_year, end_year)``. ``cap`` mirrors the
    10 000-result enumeration ceiling. ``missing`` IDs are returned by search but 404 on fetch.
    """

    def __init__(
        self,
        objects: list[tuple[int, int, int]],
        *,
        cap: int = 10_000,
        missing: set[int] | None = None,
    ) -> None:
        self.objects = {oid: (begin, end) for oid, begin, end in objects}
        self.cap = cap
        self.missing = missing or set()
        self.requests_made = 0
        self.fetched: list[int] = []

    def _matching(self, date_begin: int | None, date_end: int | None) -> list[int]:
        return sorted(
            oid
            for oid, (begin, end) in self.objects.items()
            if (date_begin is None or begin >= date_begin) and (date_end is None or end <= date_end)
        )

    async def search_total(self, query, *, date_begin=None, date_end=None) -> int:
        self.requests_made += 1
        return len(self._matching(date_begin, date_end))

    async def search_all_ids(
        self, query, *, date_begin=None, date_end=None, allow_truncation=False
    ) -> list[int]:
        self.requests_made += 1
        ids = self._matching(date_begin, date_end)
        if len(ids) > self.cap and not allow_truncation:
            raise TooManyResultsError(len(ids), self.cap)
        return ids[: self.cap]

    async def get_object(self, object_id: int) -> MetObject | None:
        self.requests_made += 1
        self.fetched.append(object_id)
        if object_id in self.missing:
            return None
        begin, end = self.objects[object_id]
        return MetObject.model_validate(
            {
                "objectID": object_id,
                "objectBeginDate": begin,
                "objectEndDate": end,
                "title": f"Object {object_id}",
            }
        )


# A bread-shaped collection: a handful of modern works, one object whose date range spans
# five centuries (1520-1999, like Met object 471028), and a large tail of older material.
MODERN = [
    (299320, 2010, 2010),
    (284537, 1998, 1998),
    (748765, 1997, 1998),
    (861237, 1982, 1992),
    (266408, 1989, 1989),
    (632941, 1987, 1987),
    (484400, 1984, 1984),
]
SPANNING = (471028, 1520, 1999)
OLD_TAIL = [(1000 + i, 1900 + i % 50, 1900 + i % 50) for i in range(200)]


def ids(response) -> list[int]:
    return [work.object_id for work in response.results]


class TestTopRecent:
    async def test_returns_top_five_including_spanning_object(self, settings: Settings):
        client = FakeMetClient([*MODERN, SPANNING, *OLD_TAIL])
        service = WorksService(client, settings)

        response = await service.top_recent("bread", 5)

        assert ids(response) == [299320, 471028, 284537, 748765, 861237]
        assert response.exact is True
        assert response.total_matches == len(MODERN) + 1 + len(OLD_TAIL)
        # The whole point: we did not fetch the 200-object tail, and the whole request
        # stayed within a modest upstream budget (the Met's edge throttles bursty clients).
        assert len(client.fetched) < 20
        assert client.requests_made <= 30

    async def test_results_are_well_formed_works(self, settings: Settings):
        service = WorksService(FakeMetClient(MODERN), settings)
        response = await service.top_recent("bread", 2)
        first = response.results[0]
        assert first.title == "Object 299320"
        assert first.object_end_date == 2010
        assert response.query == "bread"

    async def test_zero_matches(self, settings: Settings):
        service = WorksService(FakeMetClient([]), settings)
        response = await service.top_recent("xyzzy", 5)
        assert response.results == []
        assert response.total_matches == 0
        assert response.exact is True

    async def test_fewer_matches_than_limit_returns_them_all(self, settings: Settings):
        service = WorksService(FakeMetClient(MODERN[:3]), settings)
        response = await service.top_recent("bread", 5)
        assert ids(response) == [299320, 284537, 748765]
        assert response.exact is True

    async def test_missing_object_records_are_dropped(self, settings: Settings):
        client = FakeMetClient([*MODERN, SPANNING], missing={299320})
        response = await WorksService(client, settings).top_recent("bread", 5)
        assert 299320 not in ids(response)
        assert ids(response)[0] == 471028

    async def test_more_matches_than_enumerable_falls_back_to_window(self, settings: Settings):
        # 60 objects, cap of 50: the complement trick is impossible, so only objects that
        # *begin* at or after the threshold are considered and the response says so.
        objects = [(i, 1900 + i, 1900 + i) for i in range(60)] + [SPANNING]
        client = FakeMetClient(objects, cap=50)
        response = await WorksService(client, settings).top_recent("art", 5)
        assert response.exact is False
        assert ids(response) == [59, 58, 57, 56, 55]

    async def test_oversized_candidate_set_falls_back_to_window(self):
        settings = Settings(_env_file=None, warm_cache=False, max_candidates=10)
        spanners = [(500 + i, 1000, 1995) for i in range(20)]
        recent = [(600 + i, 1990 + i, 1990 + i) for i in range(10)]
        client = FakeMetClient(spanners + recent)
        response = await WorksService(client, settings).top_recent("ceramic", 5)
        assert response.exact is False
        assert ids(response) == [609, 608, 607, 606, 605]
        assert len(client.fetched) <= 10


class TestQueryHandling:
    async def test_blank_query_is_rejected(self, settings: Settings):
        service = WorksService(FakeMetClient(MODERN), settings)
        with pytest.raises(ValueError):
            await service.top_recent("   ", 5)

    async def test_non_positive_limit_is_rejected(self, settings: Settings):
        service = WorksService(FakeMetClient(MODERN), settings)
        with pytest.raises(ValueError):
            await service.top_recent("bread", 0)

    async def test_whitespace_is_normalised(self, settings: Settings):
        service = WorksService(FakeMetClient(MODERN), settings)
        response = await service.top_recent("  sour   dough ", 5)
        assert response.query == "sour dough"


class TestCaching:
    async def test_repeat_query_makes_no_upstream_requests(self, settings: Settings):
        client = FakeMetClient([*MODERN, SPANNING, *OLD_TAIL])
        service = WorksService(client, settings)
        first = await service.top_recent("bread", 5)
        made = client.requests_made
        second = await service.top_recent("bread", 5)
        assert second == first
        assert client.requests_made == made

    async def test_cache_key_ignores_case(self, settings: Settings):
        client = FakeMetClient(MODERN)
        service = WorksService(client, settings)
        await service.top_recent("bread", 5)
        made = client.requests_made
        await service.top_recent("BREAD", 5)
        assert client.requests_made == made

    async def test_different_limits_are_cached_separately(self, settings: Settings):
        client = FakeMetClient(MODERN)
        service = WorksService(client, settings)
        await service.top_recent("bread", 5)
        made = client.requests_made
        assert len((await service.top_recent("bread", 2)).results) == 2
        assert client.requests_made > made
