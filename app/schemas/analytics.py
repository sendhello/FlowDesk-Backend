"""Analytics schemas (UC-10; US-15, US-16)."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.models.enums import IncidentStatus, Severity


class VolumeBucket(BaseModel):
    """One bar. `week_start` is the Monday of that week IN THE REPORTING TIMEZONE."""

    week_start: date
    count: int


class VolumeSeries(BaseModel):
    """UC-10 steps 2-3: incidents created per week.

    Deliberately NOT `Page[VolumeBucket]`. Paginating a fixed, bounded set of at most 53
    buckets is meaningless, and the shared `limit` default of 50 would SILENTLY TRUNCATE a
    53-week request into a wrong-looking chart. The `data` key is kept so an "unwrap .data"
    helper on the frontend still works; window metadata replaces `pagination`.

    `from`/`to` echo the EFFECTIVE window: it is snapped outward to whole ISO weeks and may
    therefore be wider than what was requested, so the axis can be labelled honestly.
    `timezone` makes the bucketing self-documenting — a chart can never be misread as UTC
    weeks.

    An empty organisation is a 200 with all-zero buckets (UC-10 E1), never a 404.
    """

    data: list[VolumeBucket]
    timezone: str
    # `from` is a Python keyword. serialization_alias, not alias: this field is
    # response-only, and FastAPI serialises response models with by_alias=True, so the wire
    # key is "from" while the constructor keyword stays `from_=`.
    from_: date = Field(serialization_alias="from")
    to: date
    severity: Severity | None = None


class StatusCount(BaseModel):
    status: IncidentStatus
    count: int


class StatusDistribution(BaseModel):
    """UC-10 step 4.

    `data` always holds exactly one entry per IncidentStatus, in enum declaration order,
    including zeros. `total` makes UC-10 E1 ("No incident data available yet") a
    single-field check rather than a client-side sum.
    """

    data: list[StatusCount]
    total: int
