"""Incident analytics (UC-10; US-15, US-16).

TIMEZONE — the thing that makes this module non-trivial
--------------------------------------------------------
`incidents.created_at` is `timestamptz`. `date_trunc('week', <timestamptz>)` renders the
value in the SESSION TimeZone, which is UTC on Fly. An incident submitted Monday
2026-07-27 09:00 Melbourne (AEST, UTC+10) is Sunday 2026-07-26 23:00 UTC, and `date_trunc`
is ISO (Monday-based), so it would land in the bucket starting 2026-07-20 instead of
2026-07-27. Every Monday morning would be systematically misfiled into the previous week.

So every query converts explicitly with AT TIME ZONE, per statement, with the zone as a
bind parameter. NOT `SET TimeZone` on the session: workflow_service is documented to
behave identically under a session or a transaction-mode pooler, and a session-level SET
does not — under transaction pooling it can leak into another request or silently not
apply.

SCOPE — why this does not reuse incident_service._visibility_conds
-------------------------------------------------------------------
Analytics aggregate EVERY incident in the tenant, which is strictly more than a staff or
reviewer caller may read. Reusing the visibility predicate would look like the safe choice
and would be wrong twice over: it would hand a reviewer a chart of their own queue labelled
as organisation analytics, and the moment it were widened it would leak counts of incidents
the caller cannot open (NFR-12). The endpoints are admin-only instead, and the scope
predicate here is tenant-level only.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import Date, String, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ValidationError
from app.models.enums import IncidentStatus, Severity
from app.models.incident import Incident
from app.models.tenant import Tenant

if TYPE_CHECKING:  # pragma: no cover
    from app.api.deps import TenantScope

# UC-10 specifies neither a default range nor a maximum. 12 weeks matches the project's own
# build cycle and fits a dashboard card without scrolling; 53 weeks is the cap that stops
# `?from=1000-01-01` asking for tens of thousands of buckets. Both are pending confirmation
# from the product owner.
DEFAULT_WEEKS: int = 12
MAX_WEEKS: int = 53


def reporting_tz(tz_name: str | None = None) -> ZoneInfo:
    """The zone to report in: the tenant's if one applies, else the platform default.

    `tz_name` is optional throughout this module so the pure date helpers stay callable
    without a database, which is what `tests/test_reporting_window.py` exercises. When it
    is omitted the behaviour is exactly what it was before tenants had a timezone.

    Both values are validated before they reach here — the platform default at start-up
    (`app.core.config`), a tenant's on write (`settings_service`) — so this never has to
    defend against a bad name.
    """
    return ZoneInfo(tz_name or settings.reporting_timezone)


def today_local(tz_name: str | None = None) -> date:
    """Today in the reporting timezone.

    NOT `date.today()`: that reads the server clock, which is UTC on Fly, and would drop
    the current day for the ten hours each night that Melbourne is already tomorrow.
    """
    return datetime.now(reporting_tz(tz_name)).date()


async def resolve_timezone(
    db: AsyncSession, *, scope: "TenantScope", target_tenant_id: uuid.UUID | None
) -> str:
    """Which timezone this particular query should be bucketed in.

    One organisation in the result set means that organisation's own zone, so a Perth
    tenant sees Perth weeks. A System Admin querying across every tenant has no single
    answer available — the same incident would belong to different weeks depending on
    whose zone was chosen — so the platform default is used and echoed in the response
    rather than a tenant's zone being picked arbitrarily.

    Mirrors `_scope_conds` exactly: whatever that narrows the rows to is what this reads
    the zone from. The two must not disagree, or the buckets would be computed in a zone
    belonging to a tenant whose rows were excluded.
    """
    tenant_id = target_tenant_id if scope.is_system_admin else scope.tenant_id
    if tenant_id is None:
        return settings.reporting_timezone
    tz_name = await db.scalar(select(Tenant.timezone).where(Tenant.id == tenant_id))
    return tz_name or settings.reporting_timezone


def week_start(day: date) -> date:
    """The Monday on or before `day`.

    Must agree with PostgreSQL's `date_trunc('week', ...)`, which is ISO and therefore
    Monday-based — exactly `date.weekday() == 0`. Pinned against the database by
    tests/test_analytics_volume.py::test_week_start_matches_postgres_date_trunc.
    """
    return day - timedelta(days=day.weekday())


def resolve_window(
    week_from: date | None, week_to: date | None, tz_name: str | None = None
) -> tuple[date, date]:
    """Apply defaults, validate, and snap OUTWARD to whole ISO weeks.

    Snapping outward matters: a range that starts mid-week would make the first and last
    bars silently partial, so the trend would lie. The caller echoes the snapped window
    back in the response, which is why it can be wider than what was requested.

    Raises ValidationError with `invalid_date_range` or `date_range_too_large`.
    """
    resolved_to = week_to if week_to is not None else today_local(tz_name)
    resolved_from = (
        week_from
        if week_from is not None
        else week_start(resolved_to) - timedelta(weeks=DEFAULT_WEEKS - 1)
    )

    if resolved_from > resolved_to:
        raise ValidationError(
            "The start date must not be after the end date.",
            details={"reason": "invalid_date_range"},
        )

    first, last = week_start(resolved_from), week_start(resolved_to)
    weeks = (last - first).days // 7 + 1
    if weeks > MAX_WEEKS:
        raise ValidationError(
            f"The requested date range is too large. Request at most {MAX_WEEKS} weeks.",
            details={"reason": "date_range_too_large", "max_weeks": MAX_WEEKS},
        )
    return first, last


def window_bounds(
    week_from: date, week_to: date, tz_name: str | None = None
) -> tuple[datetime, datetime]:
    """Half-open [lower, upper) bounds as timezone-aware datetimes.

    Filtering on the raw column keeps the predicate sargable on
    `ix_incidents_tenant_created_at`; filtering on the bucket expression would not be.
    Half-open so an incident at exactly midnight is never counted in two weeks.

    Midnight is never ambiguous in Australia — DST transitions happen at 02:00/03:00 local
    — so `datetime.combine(..., tzinfo=tz)` needs no `fold` disambiguation.
    """
    tz = reporting_tz(tz_name)
    lower = datetime.combine(week_from, time.min, tzinfo=tz)
    upper = datetime.combine(week_to + timedelta(days=7), time.min, tzinfo=tz)
    return lower, upper


def week_series(week_from: date, week_to: date) -> list[date]:
    """Every Monday from `week_from` to `week_to` inclusive. Both must already be Mondays."""
    count = (week_to - week_from).days // 7 + 1
    return [week_from + timedelta(weeks=i) for i in range(count)]


def _week_expr(tz_name: str):
    """`CAST(date_trunc('week', created_at AT TIME ZONE :tz) AS DATE)`, the local Monday.

    `literal(tz_name, String)` keeps the zone a bind parameter with an explicit type — no
    string interpolation into SQL, and no reliance on the dialect's default typing to
    resolve the `timezone(text, timestamptz)` overload.
    """
    local = func.timezone(literal(tz_name, String), Incident.created_at)
    return cast(func.date_trunc("week", local), Date).label("week_start")


def _scope_conds(scope: "TenantScope", target_tenant_id: uuid.UUID | None) -> list:
    """Tenant scoping only — see the module docstring for why visibility is not applied.

    A `?tenant_id=` from a non-System-Admin is IGNORED rather than rejected, matching
    incident_service.list_incidents and user_service.list_users.
    """
    if scope.is_system_admin:
        return [] if target_tenant_id is None else [Incident.tenant_id == target_tenant_id]
    return [Incident.tenant_id == scope.tenant_id]


async def incident_volume(
    db: AsyncSession,
    *,
    scope: "TenantScope",
    target_tenant_id: uuid.UUID | None,
    severity: Severity | None,
    week_from: date,
    week_to: date,
    tz_name: str | None = None,
) -> list[tuple[date, int]]:
    """UC-10 steps 2-3 (+ step 5 severity filter): incidents created per week.

    Zero-filled: every Monday in [week_from, week_to] appears exactly once, ascending. The
    frontend must not generate the missing weeks itself — it would do so in the BROWSER's
    timezone, and a viewer in Perth or on a UTC laptop would produce a different set of
    Mondays than the server bucketed by, so the labels would not line up with the bars.
    """
    week_expr = _week_expr(tz_name or settings.reporting_timezone)
    lower, upper = window_bounds(week_from, week_to, tz_name)

    conds = _scope_conds(scope, target_tenant_id) + [
        Incident.created_at >= lower,
        Incident.created_at < upper,
    ]
    if severity is not None:
        conds.append(Incident.severity == severity)

    rows = (
        await db.execute(
            select(week_expr, func.count().label("count"))
            .where(*conds)
            .group_by(week_expr)
            .order_by(week_expr)
        )
    ).all()

    # Filled in Python: pure date arithmetic over Mondays the database already computed in
    # the reporting timezone, so no second conversion can disagree with the first.
    counts = {bucket: int(n) for bucket, n in rows}
    return [(monday, counts.get(monday, 0)) for monday in week_series(week_from, week_to)]


async def status_distribution(
    db: AsyncSession,
    *,
    scope: "TenantScope",
    target_tenant_id: uuid.UUID | None,
) -> dict[IncidentStatus, int]:
    """UC-10 step 4: the CURRENT count of incidents in each status.

    No date filter, deliberately. UC-10 step 4 says "current" while UC-10 A1 implies a
    date change re-renders both charts — a direct contradiction in the SRS. Step 4 wins:
    US-16 asks for "operational load", and a date-windowed version answers a different
    question (the status of incidents *created* in that window). Pending confirmation.

    Zero-filled over the whole enum in declaration order, so the response always has three
    entries and the frontend never branches on a missing key.
    """
    rows = (
        await db.execute(
            select(Incident.status, func.count().label("count"))
            .where(*_scope_conds(scope, target_tenant_id))
            .group_by(Incident.status)
        )
    ).all()

    counts: dict[IncidentStatus, int] = {state: 0 for state in IncidentStatus}
    counts.update({state: int(n) for state, n in rows})
    return counts
