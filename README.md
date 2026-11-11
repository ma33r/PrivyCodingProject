# Met Bread API

An API server that searches [The Metropolitan Museum of Art's open-access collection](https://metmuseum.github.io/)
for a term (default `bread`) and returns the most recent matching works, ordered by
`objectEndDate` descending.

```
GET /api/works?q=bread&limit=5
```

Both parameters are optional: `q` defaults to `bread` and must be ASCII; `limit` defaults to 5
and must be between 1 and 20.

## Quick start

Requires Python 3.12.

```bash
make install            # creates .venv and installs the package with dev tools
make run                # serves on http://127.0.0.1:8000
make demo               # in another terminal: walks through the endpoints and error cases
```

Or by hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m met_bread
curl 'localhost:8000/api/works?q=bread&limit=5'
```

Interactive OpenAPI docs are served at `/docs`. All settings are environment variables with
the `MET_` prefix; see [.env.example](.env.example).

## API

`GET /api/works` with optional `q` (1 to 100 ASCII characters, not blank, default `bread`)
and optional `limit` (integer, 1 to 20, default 5).

```json
{
  "query": "bread",
  "total_matches": 2055,
  "exact": true,
  "latency_ms": 1437.52,
  "results": [
    {
      "object_id": 299320,
      "title": "The Fae and Doris Caudill Family Eating Dinner in Their Dugout, Pie Town, New Mexico",
      "artist": "Debbie Grossman",
      "object_date": "2010",
      "object_begin_date": 2010,
      "object_end_date": 2010,
      "medium": "Inkjet print",
      "department": "Photographs",
      "image_url": null,
      "object_url": "https://www.metmuseum.org/art/collection/search/299320"
    }
  ]
}
```

| Status | Meaning |
|---|---|
| 200 | Success. `results` may be shorter than `limit` if fewer works match. |
| 400 | Malformed input: `limit` non-integer or out of bounds; `q` blank, too long or containing non-ASCII characters. The body lists the offending parameter. |
| 502 | The Met API failed after retries. |
| 503 | The Met's bot protection blocked us even after waiting 3, 7 and 15 seconds between retries. `Retry-After` says when to try again. |

`exact` is `true` when every matching object was considered. It is `false` only for terms
with more than 10 000 matches, where the Met API cannot enumerate the full result set (see
below).

`latency_ms` is the server-side time spent on that request. A cold query spends it talking to
the Met; a cached one reports a few milliseconds.

`GET /health` returns `{"status": "ok"}`.

## How it works

The Met's search endpoint is the whole problem. It returns object IDs only, with no dates
and no sort, and each object's dates live behind a separate request. `bread` matches 2 055
objects, so "fetch everything and sort" means 2 055 requests per query, tens of seconds, and
a block from the Met's edge protection.

The only lever is the search endpoint's date filter, and it has a quirk: it is a
*containment* filter. `dateBegin=Y&dateEnd=9999` returns objects whose begin date **and** end
date fall inside the window. An object dated "1520 or 19th–20th century" (begin 1520, end
1999) is invisible to every window that starts after 1520, yet for `bread` it is the second
most recent work.

The complement is expressible, though. "Ended before Y" is exactly the window
`[-9999, Y-1]`, so:

```
count(objectEndDate >= Y)  =  total  -  count_in([-9999, Y-1])
ids_with(objectEndDate >= Y) =  all_ids  -  ids_in([-9999, Y-1])
```

Both are precise, spanning objects included. One request then goes like this
([service.py](src/met_bread/service.py)):

1. **Count** the matches (1 request).
2. **Bisect** on the count above to find the latest year `Y` with at least `limit` matches
   ending in `Y` or later ([ranking.py](src/met_bread/ranking.py), about 15 requests).
3. **Subtract** the IDs ending before `Y` from all matching IDs (2 paged ID listings).
4. **Fetch** only the remaining candidates and sort by their real `objectEndDate`.

For `bread` that is 30 requests and about 1.5 seconds cold; the result is cached for an hour
and the default query is warmed at startup, so repeat requests take milliseconds.

**The 10 000 ceiling.** The Met refuses `offset + limit > 10 000`, so for terms with more
matches than that the ID subtraction is impossible. The service falls back to the window
`[Y, 9999]` alone, which can miss objects that began before `Y`, and reports `exact: false`.
A second safety valve (`MET_MAX_CANDIDATES`) does the same if an unusual number of objects
straddle the threshold.

### Being a good citizen of the Met API

Measured while building this, not taken from the docs:

- `/v1/search` was retired on 2026-10-01 (HTTP 410). Only `/v1.1/search` works.
- A lone `dateBegin` without `dateEnd` is silently ignored, so the client refuses to send one.
- An empty `q` returns the entire collection; the API rejects blank queries with a 400.
- The documented limit is 80 requests/second, but the edge protection (Incapsula) blocks a
  client that sends roughly a hundred requests within half a minute, serving an HTML 403 for
  a minute or so.

So the client ([met_client.py](src/met_bread/met_client.py)) identifies itself with a
User-Agent, caps in-flight requests, smooths the outgoing rate, and retries transient
failures with short jittered backoff. A 403 is treated as throttling and gets a longer,
increasing wait-and-retry schedule (3, 7, then 15 seconds; `MET_THROTTLE_BACKOFF_S`). If the
block outlasts that, the client enters a 30-second cooldown during which it fails fast, and
our own callers get a 503 with `Retry-After`. The cache means a busy service mostly never
reaches the Met at all.

## Project layout

```
src/met_bread/
  api.py          FastAPI app factory, routes, error mapping
  service.py      Orchestration: threshold, candidates, fetch, rank, cache
  ranking.py      Pure logic: bisection oracle and sort order
  met_client.py   Met API client: paging, retries, pacing, cooldown
  pacing.py       Non-banking rate limiter
  cache.py        TTL cache with request coalescing
  models.py       Pydantic models (Met responses and our contract)
  config.py       Settings (MET_* environment variables)
tests/            Unit tests with mocked HTTP; one opt-in live test
```

## Development

```bash
make test    # unit tests, no network
make lint    # ruff + mypy (strict)
make live    # one real run against the Met (MET_LIVE=1)
```

Tests were written before each module. HTTP is mocked with respx; the service is tested
against an in-memory fake that reproduces the Met's containment-filter semantics, including
the spanning-object case. CI runs lint, type-check and tests on every push and pull request.

## Trade-offs and what I'd do next

- **Exactness over latency.** The complement step costs two paged ID listings per cold query.
  A window-only approach would be cheaper and wrong for spanning objects.
- **Sequential bisection over parallel probing.** Requests are paced anyway, so parallel
  probes would only cost more of the Met's request budget for no latency gain.
- **In-process cache.** Fine for one instance; swap for Redis to scale out.
- **Bounded patience on throttling.** A blocked request waits up to 25 seconds across three
  retries before giving up, so callers see slow answers rather than failures during short
  blocks. Longer blocks (the ones observed lasted about a minute) still end in a 503 with
  `Retry-After`, and the cooldown stops other requests from each paying the 25 seconds.
- **400, not 422, for bad input.** FastAPI defaults to 422, which comes from WebDAV and is
  best suited to semantically invalid request bodies. For malformed query parameters, 400 is
  the conventional status every client understands; one handler maps all validation errors
  to it so the API never mixes the two.
- **Not done:** rate limiting of our own callers, authentication, a frontend, Docker image.
