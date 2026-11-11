"""Entry point: ``python -m met_bread`` or the ``met-bread`` console script."""

from __future__ import annotations

import logging

import uvicorn

from met_bread.config import Settings


def main() -> None:
    """Run the API server with settings taken from the environment."""
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every upstream request at INFO (dozens per query); our own summary lines
    # and the Met-throttling warnings are what operators actually need to see.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        "met_bread.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
