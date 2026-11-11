"""HTTP layer tests. The service is faked, so these cover routing, validation and error mapping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from met_bread.api import create_app
from met_bread.met_client import MetThrottledError, MetUpstreamError
from met_bread.models import Work, WorksResponse
from met_bread.service import WorksService

SAMPLE = WorksResponse(
    query="bread",
    total_matches=1,
    exact=True,
    results=[
        Work(
            object_id=299320,
            title="The Fae and Doris Caudill Family Eating Dinner",
            artist="Debbie Grossman",
            object_date="2010",
            object_begin_date=2010,
            object_end_date=2010,
            medium="Inkjet print",
            department="Photographs",
            image_url=None,
            object_url="https://www.metmuseum.org/art/collection/search/299320",
        )
    ],
)


class FakeService:
    def __init__(self, response: WorksResponse = SAMPLE, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def top_recent(self, query: str, limit: int) -> WorksResponse:
        self.calls.append((query, limit))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def make_client(settings):
    def factory(service) -> TestClient:
        return TestClient(create_app(settings=settings, service=service))

    return factory


def test_query_defaults_to_bread(make_client):
    service = FakeService()
    with make_client(service) as http:
        response = http.get("/api/works", params={"limit": 5})
    assert response.status_code == 200
    assert service.calls == [("bread", 5)]
    body = response.json()
    assert set(body) == {"query", "total_matches", "exact", "results"}
    assert body["results"][0]["object_id"] == 299320
    assert body["results"][0]["image_url"] is None


def test_custom_query_and_limit_are_passed_through(make_client):
    service = FakeService()
    with make_client(service) as http:
        response = http.get("/api/works", params={"q": "cheese", "limit": 3})
    assert response.status_code == 200
    assert service.calls == [("cheese", 3)]


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"limit": 5, "q": ""}, id="blank-q"),
        pytest.param({"limit": 5, "q": "   "}, id="whitespace-q"),
        pytest.param({"limit": 5, "q": "a" * 101}, id="q-too-long"),
        pytest.param({"limit": 5, "q": "br\u00ebad"}, id="q-latin-accent"),
        pytest.param({"limit": 5, "q": "\u30d1\u30f3"}, id="q-non-latin-script"),
        pytest.param({"limit": 5, "q": "bread \U0001f35e"}, id="q-emoji"),
        pytest.param({"q": "bread"}, id="limit-missing"),
        pytest.param({"q": "bread", "limit": 0}, id="limit-too-small"),
        pytest.param({"q": "bread", "limit": 21}, id="limit-too-large"),
        pytest.param({"q": "bread", "limit": "five"}, id="limit-not-an-integer"),
        pytest.param({"q": "bread", "limit": "2.5"}, id="limit-not-whole"),
    ],
)
def test_malformed_input_is_a_400_and_never_reaches_the_service(make_client, params):
    service = FakeService()
    with make_client(service) as http:
        response = http.get("/api/works", params=params)
    assert response.status_code == 400
    body = response.json()
    assert "detail" in body
    # The error names the offending parameter so callers can fix it.
    assert "q" in str(body["detail"]) or "limit" in str(body["detail"])
    assert service.calls == []


def test_ascii_punctuation_in_query_is_allowed(make_client):
    service = FakeService()
    with make_client(service) as http:
        response = http.get("/api/works", params={"q": "bread & butter, 1900's", "limit": 5})
    assert response.status_code == 200
    assert service.calls == [("bread & butter, 1900's", 5)]


def test_limit_bounds_are_inclusive(make_client):
    service = FakeService()
    with make_client(service) as http:
        assert http.get("/api/works", params={"limit": 1}).status_code == 200
        assert http.get("/api/works", params={"limit": 20}).status_code == 200
    assert [limit for _, limit in service.calls] == [1, 20]


def test_upstream_failure_maps_to_502(make_client):
    service = FakeService(error=MetUpstreamError("Met API request failed after 3 attempts"))
    with make_client(service) as http:
        response = http.get("/api/works", params={"limit": 5})
    assert response.status_code == 502
    assert "Met" in response.json()["detail"]


def test_throttled_upstream_maps_to_503_with_retry_after(make_client):
    service = FakeService(error=MetThrottledError(retry_after_s=30))
    with make_client(service) as http:
        response = http.get("/api/works", params={"limit": 5})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert "rate" in response.json()["detail"].lower()


def test_health_endpoint(make_client):
    with make_client(FakeService()) as http:
        response = http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_the_endpoint(make_client):
    with make_client(FakeService()) as http:
        schema = http.get("/openapi.json").json()
    assert "/api/works" in schema["paths"]


def test_app_builds_a_real_service_when_none_is_injected(settings):
    with TestClient(create_app(settings=settings)) as http:
        assert isinstance(http.app.state.service, WorksService)
