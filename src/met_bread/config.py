"""Runtime configuration.

All knobs are read from the environment with the ``MET_`` prefix (or a local ``.env`` file),
so deployments can tune concurrency, cache lifetime and limits without code changes.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with production-safe defaults."""

    model_config = SettingsConfigDict(env_prefix="MET_", env_file=".env", extra="ignore")

    # Upstream Met API. Search lives under v1.1 (v1 was retired 2026-10-01); objects under v1.
    base_url: str = "https://collectionapi.metmuseum.org/public/collection"
    request_timeout_s: float = Field(10.0, gt=0)
    # Identify ourselves to the Met; anonymous bursty traffic trips their bot protection.
    user_agent: str = "met-bread-api/0.1 (+https://github.com/ma33r/PrivyCodingProject)"

    # Politeness / resilience. The Met documents an 80 req/s ceiling, but its edge protection
    # blocks (HTTP 403) a client that sends roughly a hundred requests within half a minute.
    # So: modest concurrency, a smoothed request rate, short jittered retries for transient
    # failures, and for a 403 a longer, increasing wait-and-retry schedule followed by a
    # cooldown during which we stop sending altogether (see met_client.py).
    # Process-wide: one MetClient (and one semaphore) serves every incoming API request, so
    # this bounds the total number of in-flight Met requests, not the number per API call.
    max_concurrency: int = Field(6, ge=1)
    max_requests_per_second: float = Field(40.0, ge=0)  # 0 disables pacing (tests)
    max_retries: int = Field(3, ge=0)
    backoff_base_s: float = Field(0.5, ge=0)
    # Seconds to wait before each retry of a request the Met answered with 403. Exhausting
    # the list raises MetThrottledError and starts the cooldown. Empty list = fail fast.
    throttle_backoff_s: list[float] = Field(default_factory=lambda: [3.0, 7.0, 15.0])
    throttle_cooldown_s: float = Field(30.0, ge=0)

    @field_validator("throttle_backoff_s")
    @classmethod
    def _waits_are_non_negative(cls, waits: list[float]) -> list[float]:
        if any(wait < 0 for wait in waits):
            raise ValueError("throttle_backoff_s entries must be >= 0")
        return waits

    # Search pagination constraints imposed by the Met: pages of at most 500, and
    # offset + limit may not exceed 10 000 (so larger result sets cannot be fully enumerated).
    page_size: int = Field(500, ge=1, le=500)
    max_enumerable: int = Field(10_000, ge=1)
    # Safety valve on how many object records one request may fetch (see service.py).
    max_candidates: int = Field(600, ge=1)

    # Our API's behaviour.
    default_query: str = "bread"
    default_limit: int = Field(5, ge=1)
    max_limit: int = Field(20, ge=1)
    max_query_length: int = Field(100, ge=1)
    cache_ttl_s: float = Field(3600.0, ge=0)
    warm_cache: bool = True

    # Server.
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65535)
    log_level: str = "INFO"
