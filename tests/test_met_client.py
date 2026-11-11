"""MetClient tests. All HTTP is mocked with respx; nothing here touches the network."""

import asyncio

import httpx
import pytest

from met_bread.met_client import MetClient, MetUpstreamError, TooManyResultsError
from tests.conftest import load_fixture, load_fixture_text

SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
OBJECTS = "https://collectionapi.metmuseum.org/public/collection/v1/objects"


@pytest.fixture
async def client(settings):
    async with httpx.AsyncClient() as http:
        yield MetClient(http, settings)


class TestSearch:
    async def test_sends_user_agent_and_query_params(self, client, respx_mock):
        route = respx_mock.get(SEARCH).respond(json=load_fixture("search_bread_limit5.json"))
        resp = await client.search_ids("bread", limit=5)
        assert resp.total == 2055
        request = route.calls.last.request
        assert request.headers["user-agent"].startswith("met-bread-api")
        assert request.url.params["q"] == "bread"
        assert request.url.params["limit"] == "5"
        assert "dateBegin" not in request.url.params

    async def test_date_filter_is_sent_as_a_pair(self, client, respx_mock):
        route = respx_mock.get(SEARCH).respond(json={"total": 0, "objectIDs": None})
        await client.search_ids("bread", date_begin=1980, date_end=9999)
        params = route.calls.last.request.url.params
        assert params["dateBegin"] == "1980"
        assert params["dateEnd"] == "9999"

    async def test_half_specified_date_filter_is_rejected(self, client):
        # The Met silently ignores a lone dateBegin, which would quietly return everything.
        with pytest.raises(ValueError):
            await client.search_ids("bread", date_begin=1980)

    async def test_search_total_requests_a_single_id(self, client, respx_mock):
        route = respx_mock.get(SEARCH).respond(json={"total": 2055, "objectIDs": [688655]})
        assert await client.search_total("bread") == 2055
        assert route.calls.last.request.url.params["limit"] == "1"

    async def test_zero_results(self, client, respx_mock):
        respx_mock.get(SEARCH).respond(json=load_fixture("search_no_results.json"))
        assert await client.search_total("xyzzy") == 0
        assert await client.search_all_ids("xyzzy") == []


def paged_collection(total: int, page_size: int = 500):
    """Responder that serves IDs 0..total-1 in pages, honouring offset/limit like the Met."""

    def responder(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", page_size))
        ids = list(range(offset, min(offset + limit, total)))
        return httpx.Response(200, json={"total": total, "objectIDs": ids or None})

    return responder


class TestSearchAllIds:
    async def test_pages_until_total_is_reached(self, client, respx_mock):
        route = respx_mock.get(SEARCH).mock(side_effect=paged_collection(1200))
        ids = await client.search_all_ids("bread")
        assert ids == list(range(1200))
        assert route.call_count == 3  # offsets 0, 500, 1000

    async def test_raises_when_total_exceeds_enumerable_cap(self, client, respx_mock):
        respx_mock.get(SEARCH).mock(side_effect=paged_collection(10_001))
        with pytest.raises(TooManyResultsError) as excinfo:
            await client.search_all_ids("art")
        assert excinfo.value.total == 10_001

    async def test_can_truncate_at_the_cap_when_asked(self, client, respx_mock):
        route = respx_mock.get(SEARCH).mock(side_effect=paged_collection(10_001))
        ids = await client.search_all_ids("art", allow_truncation=True)
        assert len(ids) == 10_000
        assert route.call_count == 20
        # The Met rejects offset + limit > 10 000; make sure we never ask for that.
        for call in route.calls:
            params = call.request.url.params
            assert int(params["offset"]) + int(params["limit"]) <= 10_000


class TestGetObject:
    async def test_parses_record(self, client, respx_mock):
        respx_mock.get(f"{OBJECTS}/471028").respond(json=load_fixture("object_471028.json"))
        obj = await client.get_object(471028)
        assert obj is not None
        assert obj.object_end_date == 1999

    async def test_returns_none_on_404_without_retrying(self, client, respx_mock):
        route = respx_mock.get(f"{OBJECTS}/999999999").respond(
            404, json=load_fixture("object_not_found.json")
        )
        assert await client.get_object(999999999) is None
        assert route.call_count == 1


class TestResilience:
    async def test_retries_bot_protection_403_html_page(self, client, respx_mock):
        route = respx_mock.get(f"{OBJECTS}/1")
        route.side_effect = [
            httpx.Response(
                403,
                text=load_fixture_text("blocked_403.html"),
                headers={"content-type": "text/html"},
            ),
            httpx.Response(200, json={"objectID": 1, "objectEndDate": 1900}),
        ]
        obj = await client.get_object(1)
        assert obj is not None and obj.object_end_date == 1900
        assert route.call_count == 2
        assert client.requests_made == 2

    async def test_retries_transport_errors(self, client, respx_mock):
        route = respx_mock.get(f"{OBJECTS}/1")
        route.side_effect = [
            httpx.ConnectError("connection reset"),
            httpx.Response(200, json={"objectID": 1}),
        ]
        assert await client.get_object(1) is not None
        assert route.call_count == 2

    async def test_non_json_200_body_is_retried(self, client, respx_mock):
        route = respx_mock.get(f"{OBJECTS}/1")
        route.side_effect = [
            httpx.Response(
                200, text="<html>interstitial</html>", headers={"content-type": "text/html"}
            ),
            httpx.Response(200, json={"objectID": 1}),
        ]
        assert await client.get_object(1) is not None
        assert route.call_count == 2

    async def test_gives_up_after_max_retries(self, client, respx_mock, settings):
        route = respx_mock.get(f"{OBJECTS}/1").respond(503, json={"message": "down"})
        with pytest.raises(MetUpstreamError):
            await client.get_object(1)
        assert route.call_count == settings.max_retries + 1

    async def test_unexpected_4xx_is_not_retried(self, client, respx_mock):
        route = respx_mock.get(SEARCH).respond(400, json={"message": "bad request"})
        with pytest.raises(MetUpstreamError):
            await client.search_total("bread")
        assert route.call_count == 1

    async def test_in_flight_requests_are_bounded_by_semaphore(self, client, respx_mock, settings):
        inflight = {"now": 0, "max": 0}

        async def slow(request: httpx.Request) -> httpx.Response:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
            await asyncio.sleep(0.005)
            inflight["now"] -= 1
            object_id = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"objectID": object_id})

        respx_mock.get(url__regex=rf"{OBJECTS}/\d+").mock(side_effect=slow)
        results = await asyncio.gather(*(client.get_object(i) for i in range(12)))
        assert len(results) == 12
        assert 1 < inflight["max"] <= settings.max_concurrency
