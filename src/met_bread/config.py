"""Runtime configuration.

All knobs are read from the environment with the ``MET_`` prefix (or a local ``.env`` file),
so deployments can tune concurrency, cache lifetime and limits without code changes.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with production-safe defaults."""

    model_config = SettingsConfigDict(env_prefix="MET_", env_file=".env", extra="ignore")

    # Upstream Met API. Search lives under v1.1 (v1 was retired 2026-10-01); objects under v1.
    base_url: str = "https://collectionapi.metmuseum.org/public/collection"
    request_timeout_s: float = Field(10.0, gt=0)
    # Identify ourselves to the Met; anonymous bursty traffic trips their bot protection.
    user_agent: str = "met-bread-api/0.1 (+https://github.com/ma33r/PrivyCodingProject)"

    # Politeness / resilience. The Met asks for < 80 req/s; its edge 403s well before that
    # when requests arrive in a tight burst, so we keep concurrency modest and retry with backoff.
    max_concurrency: int = Field(6, ge=1)
    # Observed: ~150 requests within ~5 s earned a multi-second 403 block, so pace well below
    # that. One cold query costs ~35 requests; 0 disables pacing (tests).
    max_requests_per_second: float = Field(15.0, ge=0)
    max_retries: int = Field(3, ge=0)
    backoff_base_s: float = Field(0.5, ge=0)

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
