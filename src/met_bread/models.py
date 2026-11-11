"""Data models.

Two families live here:

* ``SearchResponse`` / ``MetObject`` mirror what the Met API actually returns. They are
  deliberately lenient (unknown fields ignored, nulls coerced to defaults) because the Met's
  records are sparse and inconsistent across departments.
* ``Work`` / ``WorksResponse`` are *our* API's contract: snake_case, stable, and with the
  Met's "empty string means absent" convention normalised to ``null``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchResponse(BaseModel):
    """Body of ``GET /v1.1/search``.

    The search endpoint returns IDs only (never dates), and ``objectIDs`` is ``null`` rather
    than ``[]`` when there are no matches.
    """

    model_config = ConfigDict(populate_by_name=True)

    total: int
    object_ids: list[int] = Field(default_factory=list, alias="objectIDs")

    @field_validator("object_ids", mode="before")
    @classmethod
    def _null_means_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class MetObject(BaseModel):
    """The subset of ``GET /v1/objects/{id}`` that this service consumes.

    ``objectBeginDate`` / ``objectEndDate`` are the machine-readable years (negative = BC);
    ``objectDate`` is the human display string. Ranking uses ``object_end_date`` exclusively.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    object_id: int = Field(alias="objectID")
    title: str = ""
    artist_display_name: str = Field("", alias="artistDisplayName")
    object_date: str = Field("", alias="objectDate")
    object_begin_date: int = Field(0, alias="objectBeginDate")
    object_end_date: int = Field(0, alias="objectEndDate")
    medium: str = ""
    department: str = ""
    primary_image_small: str = Field("", alias="primaryImageSmall")
    object_url: str = Field("", alias="objectURL")
    is_public_domain: bool = Field(False, alias="isPublicDomain")

    @model_validator(mode="before")
    @classmethod
    def _drop_nulls(cls, data: Any) -> Any:
        # The Met emits explicit nulls for some fields. Dropping them lets each field's
        # default apply instead of failing validation on ``None``.
        if isinstance(data, dict):
            return {key: value for key, value in data.items() if value is not None}
        return data


class Work(BaseModel):
    """A single work as returned by this API."""

    object_id: int = Field(description="Met object ID")
    title: str
    artist: str | None = Field(description="Artist display name, if known")
    object_date: str = Field(description="Human-readable date string from the Met")
    object_begin_date: int = Field(description="Earliest creation year (negative = BC)")
    object_end_date: int = Field(description="Latest creation year; results are sorted by this")
    medium: str | None
    department: str | None
    image_url: str | None = Field(description="Small primary image, if open access")
    object_url: str = Field(description="Object page on metmuseum.org")

    @classmethod
    def from_met(cls, obj: MetObject) -> Work:
        """Build the public representation from a raw Met record."""
        return cls(
            object_id=obj.object_id,
            title=obj.title,
            artist=_or_none(obj.artist_display_name),
            object_date=obj.object_date,
            object_begin_date=obj.object_begin_date,
            object_end_date=obj.object_end_date,
            medium=_or_none(obj.medium),
            department=_or_none(obj.department),
            image_url=_or_none(obj.primary_image_small),
            object_url=obj.object_url,
        )


class WorksResponse(BaseModel):
    """Response body for ``GET /api/works``."""

    query: str = Field(description="The (normalised) search term that was used")
    total_matches: int = Field(description="How many objects matched the term at the Met")
    exact: bool = Field(
        description=(
            "True when every matching object was considered. False only for terms with more "
            "than 10 000 matches, where the Met API cannot enumerate the full result set."
        )
    )
    results: list[Work] = Field(description="Up to `limit` works, most recent first")


def _or_none(value: str) -> str | None:
    """Map the Met's empty-string-for-absent convention to ``None``."""
    return value or None
