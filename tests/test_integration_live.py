"""Opt-in integration test against the real Met API.

Skipped by default so the suite stays fast and deterministic. Run with ``make live`` or
``MET_LIVE=1 pytest tests/test_integration_live.py``.
"""

from __future__ import annotations

import os

import httpx
import pytest

from met_bread.config import Settings
from met_bread.met_client import MetClient
from met_bread.service import WorksService

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("MET_LIVE") != "1", reason="set MET_LIVE=1 to hit the Met"),
]

SPANNING_OBJECT_ID = 471028  # "Christ and the Pilgrims at Emmaus", dated 1520-1999


async def test_bread_top_five_from_the_real_met():
    settings = Settings(_env_file=None, warm_cache=False)
    async with httpx.AsyncClient() as http:
        client = MetClient(http, settings)
        service = WorksService(client, settings)

        response = await service.top_recent("bread", 5)
        spanning = await client.get_object(SPANNING_OBJECT_ID)

    assert response.total_matches > 1000
    assert response.exact is True
    assert len(response.results) == 5

    end_dates = [work.object_end_date for work in response.results]
    assert end_dates == sorted(end_dates, reverse=True)

    # A window-only search ([Y, 9999]) cannot see this object because it *began* in 1520.
    # If its end date qualifies for the top five, the complement trick must have found it.
    assert spanning is not None
    returned_ids = {work.object_id for work in response.results}
    if spanning.object_end_date >= end_dates[-1]:
        assert SPANNING_OBJECT_ID in returned_ids

    # Sanity-check the whole request stayed small: a few dozen requests, not thousands.
    assert client.requests_made < 150
