from met_bread.config import Settings


def test_defaults_match_the_brief():
    s = Settings(_env_file=None)
    assert s.default_query == "bread"
    assert s.default_limit == 5
    assert s.base_url.endswith("/public/collection")


def test_env_prefix_overrides(monkeypatch):
    monkeypatch.setenv("MET_DEFAULT_QUERY", "cheese")
    monkeypatch.setenv("MET_MAX_CONCURRENCY", "2")
    s = Settings(_env_file=None)
    assert s.default_query == "cheese"
    assert s.max_concurrency == 2


def test_page_size_cannot_exceed_met_maximum():
    import pytest

    with pytest.raises(ValueError):
        Settings(_env_file=None, page_size=501)


def test_throttle_backoff_schedule_defaults_and_parses_from_env(monkeypatch):
    assert Settings(_env_file=None).throttle_backoff_s == [3.0, 7.0, 15.0]
    monkeypatch.setenv("MET_THROTTLE_BACKOFF_S", "[1, 2]")
    assert Settings(_env_file=None).throttle_backoff_s == [1.0, 2.0]


def test_throttle_backoff_rejects_negative_waits():
    import pytest

    with pytest.raises(ValueError):
        Settings(_env_file=None, throttle_backoff_s=[3, -1])
