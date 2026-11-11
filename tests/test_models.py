from met_bread.models import MetObject, SearchResponse, Work, WorksResponse
from tests.conftest import load_fixture


class TestSearchResponse:
    def test_null_object_ids_become_empty_list(self):
        resp = SearchResponse.model_validate(load_fixture("search_no_results.json"))
        assert resp.total == 0
        assert resp.object_ids == []

    def test_parses_ids_and_total(self):
        resp = SearchResponse.model_validate(load_fixture("search_bread_limit5.json"))
        assert resp.total == 2055
        assert resp.object_ids[:2] == [688655, 688653]


class TestMetObject:
    def test_parses_real_record_and_ignores_unknown_fields(self):
        obj = MetObject.model_validate(load_fixture("object_471028.json"))
        assert obj.object_id == 471028
        assert obj.object_begin_date == 1520
        assert obj.object_end_date == 1999
        assert obj.department == "The Cloisters"
        # The Met really uses en dashes in display dates; keep the assertion faithful.
        assert obj.object_date == "1520–1530 or 19th–20th century"  # noqa: RUF001

    def test_missing_fields_fall_back_to_defaults(self):
        obj = MetObject.model_validate({"objectID": 1})
        assert obj.title == ""
        assert obj.object_begin_date == 0
        assert obj.object_end_date == 0
        assert obj.primary_image_small == ""
        assert obj.is_public_domain is False

    def test_explicit_nulls_are_coerced_to_defaults(self):
        obj = MetObject.model_validate(
            {"objectID": 1, "title": None, "artistDisplayName": None, "objectEndDate": None}
        )
        assert obj.title == ""
        assert obj.artist_display_name == ""
        assert obj.object_end_date == 0


class TestWork:
    def test_from_met_maps_fields(self):
        obj = MetObject.model_validate(load_fixture("object_299320.json"))
        work = Work.from_met(obj)
        assert work.object_id == 299320
        assert work.artist == "Debbie Grossman"
        assert work.object_begin_date == 2010
        assert work.object_end_date == 2010
        assert work.object_url.endswith("/299320")
        assert work.title.startswith("The Fae and Doris Caudill Family")

    def test_empty_optional_strings_become_none(self):
        obj = MetObject.model_validate(load_fixture("object_688655.json"))
        assert obj.primary_image_small == ""  # the fixture genuinely has no image
        work = Work.from_met(obj)
        assert work.image_url is None
        assert work.artist == "Bond Bread"

        bare = Work.from_met(MetObject.model_validate({"objectID": 1}))
        assert bare.artist is None
        assert bare.medium is None
        assert bare.department is None


def test_works_response_serialises_to_plain_json():
    obj = MetObject.model_validate(load_fixture("object_299320.json"))
    resp = WorksResponse(query="bread", total_matches=1, exact=True, results=[Work.from_met(obj)])
    data = resp.model_dump(mode="json")
    assert data["query"] == "bread"
    assert data["exact"] is True
    assert data["results"][0]["object_id"] == 299320
    assert data["results"][0]["object_end_date"] == 2010


class TestLatency:
    def test_defaults_to_zero_and_serialises(self):
        resp = WorksResponse(query="bread", total_matches=0, exact=True, results=[])
        assert resp.latency_ms == 0.0
        assert resp.model_dump(mode="json")["latency_ms"] == 0.0

    def test_rejects_negative_latency(self):
        import pytest

        with pytest.raises(ValueError):
            WorksResponse(query="bread", total_matches=0, exact=True, results=[], latency_ms=-1)
