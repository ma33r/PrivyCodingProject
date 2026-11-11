import asyncio

import pytest

from met_bread.models import MetObject
from met_bread.ranking import find_threshold_year, rank_works
from tests.conftest import load_fixture


def obj(object_id: int, begin: int, end: int) -> MetObject:
    return MetObject.model_validate(
        {"objectID": object_id, "objectBeginDate": begin, "objectEndDate": end}
    )


class TestRankWorks:
    def test_sorts_by_end_date_descending(self):
        ranked = rank_works([obj(1, 1900, 1950), obj(2, 1900, 2010), obj(3, 1900, 1980)], 5)
        assert [o.object_id for o in ranked] == [2, 3, 1]

    def test_tiebreak_is_begin_date_desc_then_id_asc(self):
        ranked = rank_works([obj(1, 1990, 1998), obj(3, 1997, 1998), obj(2, 1997, 1998)], 5)
        assert [o.object_id for o in ranked] == [2, 3, 1]

    def test_respects_limit(self):
        objs = [obj(i, 1900 + i, 1900 + i) for i in range(10)]
        assert len(rank_works(objs, 5)) == 5
        assert rank_works(objs, 5)[0].object_id == 9

    def test_empty_input(self):
        assert rank_works([], 5) == []

    def test_spanning_object_ranks_by_its_end_date(self):
        # 471028 is dated 1520 to 1999; its *end* date must place it above a 1998 work.
        spanning = MetObject.model_validate(load_fixture("object_471028.json"))
        later_begin = obj(284537, 1998, 1998)
        ranked = rank_works([later_begin, spanning], 5)
        assert [o.object_id for o in ranked] == [471028, 284537]


def make_counter(begin_years: list[int]):
    """Fake `count_from` backed by a list of object begin years; records calls and concurrency."""
    calls: list[int] = []
    inflight = {"now": 0, "max": 0}

    async def count_from(year: int) -> int:
        calls.append(year)
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0)  # yield so concurrent probes overlap
        inflight["now"] -= 1
        return sum(1 for b in begin_years if b >= year)

    return count_from, calls, inflight


class TestFindThresholdYear:
    async def test_finds_latest_year_with_enough_matches(self):
        begins = [2010, 1998, 1997, 1989, 1987, 1986, 1984, 1981] + [1950] * 100
        count_from, calls, _ = make_counter(begins)
        # Exactly five objects begin in 1987 or later; only four begin in 1988 or later.
        assert await find_threshold_year(count_from, need=5) == 1987
        # A dense grid in recent centuries plus small refinement rounds: ~20 probes,
        # not a per-year scan. This is a regression guard on upstream request volume.
        assert len(calls) <= 25

    async def test_returns_none_when_collection_is_smaller_than_need(self):
        count_from, _, _ = make_counter([2000, 1900])
        assert await find_threshold_year(count_from, need=5) is None

    async def test_exactly_need_matches_in_latest_year(self):
        count_from, _, _ = make_counter([2000] * 5)
        assert await find_threshold_year(count_from, need=5) == 2000

    async def test_handles_bc_dates(self):
        count_from, _, _ = make_counter([-500] * 5 + [-2000] * 3)
        assert await find_threshold_year(count_from, need=5) == -500

    async def test_probes_run_concurrently(self):
        count_from, _, inflight = make_counter([1950] * 10)
        await find_threshold_year(count_from, need=5)
        assert inflight["max"] > 1

    async def test_rejects_non_positive_need(self):
        count_from, _, _ = make_counter([2000])
        with pytest.raises(ValueError):
            await find_threshold_year(count_from, need=0)
