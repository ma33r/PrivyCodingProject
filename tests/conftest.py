"""Shared pytest fixtures."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from met_bread.config import Settings

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Return the parsed JSON content of a recorded Met API response."""
    return json.loads((FIXTURES_DIR / name).read_text())


def load_fixture_text(name: str) -> str:
    """Return a recorded response body verbatim (for non-JSON bodies such as the 403 page)."""
    return (FIXTURES_DIR / name).read_text()


@pytest.fixture
def settings() -> Settings:
    """Settings tuned for fast, deterministic tests: no backoff sleeps, no .env, no warmup."""
    return Settings(
        _env_file=None,
        backoff_base_s=0.0,
        max_retries=2,
        max_concurrency=3,
        max_requests_per_second=0,
        cache_ttl_s=60.0,
        warm_cache=False,
    )
