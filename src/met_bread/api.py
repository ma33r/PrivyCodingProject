"""HTTP layer: a FastAPI application exposing the works endpoint.

``create_app`` is a factory rather than a module-level singleton so tests can inject a fake
service and settings, and so the process owns exactly one upstream connection pool.

Note: this module deliberately does *not* use ``from __future__ import annotations``. FastAPI
evaluates handler annotations at runtime, and the ``Annotated[..., Query(...)]`` parameters
below reference the ``settings`` closure variable, which a stringified annotation cannot see.
"""

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from met_bread import __version__
from met_bread.config import Settings
from met_bread.met_client import MetClient, MetThrottledError, MetUpstreamError
from met_bread.models import WorksResponse
from met_bread.service import WorksService

logger = logging.getLogger(__name__)


class WorksProvider(Protocol):
    """What the HTTP layer needs from the service; tests substitute a fake."""

    async def top_recent(self, query: str, limit: int) -> WorksResponse:
        """Return up to ``limit`` works for ``query``, most recent first."""
        ...


def create_app(
    settings: Settings | None = None,
    service: WorksProvider | None = None,
) -> FastAPI:
    """Build the application. Pass ``service`` to bypass the real Met client (tests)."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with _provide_service(settings, service) as provider:
            app.state.service = provider
            # Warm the default query so the very first request is served from cache.
            # Done in the background: startup must not block on, or fail because of, the Met.
            warm_task = (
                asyncio.create_task(_warm_cache(provider, settings))
                if settings.warm_cache
                else None
            )
            try:
                yield
            finally:
                if warm_task is not None:
                    warm_task.cancel()

    app = FastAPI(
        title="Met Bread API",
        version=__version__,
        description=(
            "Searches The Metropolitan Museum of Art's collection for a term and returns the "
            "most recent matching works, ordered by `objectEndDate` descending."
        ),
        lifespan=lifespan,
    )

    @app.get(
        "/api/works",
        response_model=WorksResponse,
        summary="Most recent works matching a search term",
    )
    async def get_works(
        request: Request,
        q: Annotated[
            str,
            Query(
                min_length=1,
                max_length=settings.max_query_length,
                description="Search term, matched against the Met's full-text index",
            ),
        ] = settings.default_query,
        limit: Annotated[
            int,
            Query(ge=1, le=settings.max_limit, description="How many works to return"),
        ] = settings.default_limit,
    ) -> WorksResponse:
        # ``min_length`` does not catch whitespace-only input, so check that explicitly here.
        if not q.strip():
            raise HTTPException(status_code=422, detail="q must not be blank")
        provider: WorksProvider = request.app.state.service
        return await provider.top_recent(q, limit)

    @app.get("/health", summary="Liveness probe")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(MetThrottledError)
    async def upstream_throttled(_: Request, exc: MetThrottledError) -> JSONResponse:
        # Not our outage and not permanent: tell clients exactly when to come back.
        retry_after = max(1, math.ceil(exc.retry_after_s))
        logger.warning("upstream throttled: %s", exc)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(retry_after)},
            content={
                "detail": (
                    "The Met Museum API is rate-limiting this service; "
                    f"retry in about {retry_after} seconds."
                )
            },
        )

    @app.exception_handler(MetUpstreamError)
    async def upstream_error(_: Request, exc: MetUpstreamError) -> JSONResponse:
        # The Met is a dependency we do not control: report it as a bad gateway, not our fault.
        logger.error("upstream failure: %s", exc)
        return JSONResponse(
            status_code=502,
            content={
                "detail": "The Met Museum API is unavailable right now; please retry shortly."
            },
        )

    return app


@asynccontextmanager
async def _provide_service(
    settings: Settings, injected: WorksProvider | None
) -> AsyncIterator[WorksProvider]:
    """Yield the injected service, or build the real one over a shared HTTP connection pool."""
    if injected is not None:
        yield injected
        return
    async with httpx.AsyncClient(timeout=settings.request_timeout_s) as http:
        yield WorksService(MetClient(http, settings), settings)


async def _warm_cache(provider: WorksProvider, settings: Settings) -> None:
    try:
        await provider.top_recent(settings.default_query, settings.default_limit)
        logger.info("cache warmed for default query %r", settings.default_query)
    except Exception:
        logger.warning("cache warm-up failed; the first request will be slower", exc_info=True)
