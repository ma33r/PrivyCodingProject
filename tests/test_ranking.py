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


def make_counter(end_years: list[int]):
    """Fake oracle: how many objects have objectEndDate >= year. Records every call."""
    calls: list[int] = []

    async def count_at_least(year: int) -> int:
        calls.append(year)
        return sum(1 for end in end_years if end >= year)

    return count_at_least, calls


class TestFindThresholdYear:
    async def test_finds_latest_year_with_enough_matches(self):
        ends = [2010, 1998, 1998, 1992, 1989, 1987, 1984, 1981] + [1950] * 100
        count_at_least, calls = make_counter(ends)
        # Exactly five objects end in 1989 or later; only four end in 1990 or later.
        assert await find_threshold_year(count_at_least, need=5) == 1989
        # Bisection over the Met's date range: ~15 sequential probes, never a per-year scan.
        # This is a regression guard on upstream request volume.
        assert len(calls) <= 16

    async def test_returns_none_when_collection_is_smaller_than_need(self):
        count_at_least, calls = make_counter([2000, 1900])
        assert await find_threshold_year(count_at_least, need=5) is None
        assert len(calls) == 1  # one probe at the floor settles it

    async def test_exactly_need_matches_in_latest_year(self):
        count_at_least, _ = make_counter([2000] * 5)
        assert await find_threshold_year(count_at_least, need=5) == 2000

    async def test_handles_bc_dates(self):
        count_at_least, _ = make_counter([-500] * 5 + [-2000] * 3)
        assert await find_threshold_year(count_at_least, need=5) == -500

    async def test_respects_custom_bounds(self):
        count_at_least, calls = make_counter([1995] * 5)
        assert await find_threshold_year(count_at_least, need=5, floor=1900, ceiling=2030) == 1995
        assert min(calls) >= 1900 and max(calls) <= 2030

    async def test_rejects_non_positive_need(self):
        count_at_least, _ = make_counter([2000])
        with pytest.raises(ValueError):
            await find_threshold_year(count_at_least, need=0)
