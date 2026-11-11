#!/usr/bin/env python
"""Deliberately trip the Met's bot detection through our API and watch the backoff work.

Each *cold* query (a term not yet cached) costs the Met about 30 requests, and its edge
protection blocks a client that sends roughly a hundred within half a minute. So a handful of
distinct terms in quick succession is enough. This script fires them in small waves and stops
at the first sign that the schedule is exhausted (a 503 with Retry-After), or after
``--max-queries``. It is intentionally bounded: the point is to observe one block, not to
keep hammering the Met.

Run the server in another terminal and watch its log for the warnings::

    make run                      # terminal 1
    .venv/bin/python scripts/throttle_probe.py   # terminal 2

What to look for:

* server log: ``Met edge protection returned 403 ...; waiting 3s before retry 1/3`` etc.
* this script: a response whose latency jumps by several seconds (the waits), then either a
  recovery (schedule worked) or a 503 whose ``Retry-After`` matches the cooldown.

Afterwards, give the Met a minute before running anything else against it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
import sys
import time

import httpx

# Common, distinct terms so every query is a cache miss on a fresh server.
TERMS = [
    "bread", "cheese", "wine", "apple", "fish", "horse", "dog", "cat", "lion", "sword",
    "helmet", "vase", "chair", "mirror", "candle", "boat", "river", "mountain", "moon", "sun",
    "angel", "king", "queen", "garden", "rose", "lily", "oak", "bridge", "castle", "tower",
    "mask", "drum", "flute", "harp", "coin", "ring", "bottle", "cup", "bowl", "plate",
    "dragon", "eagle", "owl", "snake", "tiger", "elephant", "camel", "deer", "wolf", "bear",
]  # fmt: skip

#: A response this much slower than the running median means a backoff wait happened inside it.
STALL_THRESHOLD_S = 2.5


async def probe(base_url: str, concurrency: int, max_queries: int) -> int:
    """Fire cold queries in waves; return 0 if throttling was observed, 1 otherwise."""
    terms = TERMS[:max_queries]
    started = time.perf_counter()
    latencies: list[float] = []
    stall_seen = False
    count = 0

    async with httpx.AsyncClient(base_url=base_url, timeout=90.0) as http:
        for wave_start in range(0, len(terms), concurrency):
            wave = terms[wave_start : wave_start + concurrency]
            responses = await asyncio.gather(
                *(_timed_get(http, term) for term in wave), return_exceptions=True
            )
            for term, outcome in zip(wave, responses, strict=True):
                count += 1
                elapsed = time.perf_counter() - started
                if isinstance(outcome, BaseException):
                    print(f"#{count:>2} {elapsed:6.1f}s  {term:<10} ERROR {outcome!r}")
                    continue
                status, seconds, body = outcome
                note = ""
                if status == 200:
                    if latencies and seconds > statistics.median(latencies) + STALL_THRESHOLD_S:
                        note = "<- stalled: backoff waits happened inside this request"
                        stall_seen = True
                    elif seconds < 0.05:
                        note = "(cached)"
                    latencies.append(seconds)
                print(
                    f"#{count:>2} {elapsed:6.1f}s  {term:<10} HTTP {status}  {seconds:6.2f}s {note}"
                )
                if status == 503:
                    retry_after = body.get("retry_after")
                    print(f"\nSchedule exhausted: 503 with Retry-After={retry_after}s.")
                    print(f"Detail: {body.get('detail')}")
                    return 0
            print(f"    ... ~{count * 30} Met requests issued so far")

    print()
    if stall_seen:
        print(
            "Backoff observed: stalled requests recovered; the Met unblocked within the schedule."
        )
        return 0
    print("No throttling observed. Try more queries or higher --concurrency.")
    return 1


async def _timed_get(http: httpx.AsyncClient, term: str) -> tuple[int, float, dict[str, object]]:
    started = time.perf_counter()
    response = await http.get("/api/works", params={"q": term, "limit": 5})
    seconds = time.perf_counter() - started
    body: dict[str, object] = {}
    with contextlib.suppress(ValueError):
        body = response.json()
    if response.status_code == 503:
        body["retry_after"] = response.headers.get("retry-after")
    return response.status_code, seconds, body


def main() -> None:
    """Parse arguments and run the probe."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=3, help="cold queries per wave")
    parser.add_argument("--max-queries", type=int, default=24, help="stop after this many")
    args = parser.parse_args()
    print(
        f"Probing {args.base_url} with waves of {args.concurrency} cold queries "
        f"(max {args.max_queries}). Watch the server log.\n"
    )
    sys.exit(asyncio.run(probe(args.base_url, args.concurrency, args.max_queries)))


if __name__ == "__main__":
    main()
