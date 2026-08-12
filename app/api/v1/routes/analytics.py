"""Analytics routes (UC-10; US-15, US-16).

Role gate mirrors routes/users.py: Tenant Admin plus System Admin. UC-10 names only Tenant
Admin, but System Admin is cross-tenant everywhere else in the API and excluding them would
make analytics the one endpoint family they cannot reach.

Staff and Reviewer are 403 for a substantive reason, not just an unmodelled role: these
endpoints aggregate every incident in the tenant, so a staff caller would learn how many
incidents exist that they are forbidden to read (NFR-12). See analytics_service.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, TenantScope, require_role, tenant_scope
from app.db.session import get_db
from app.models.enums import Role, Severity
from app.schemas.analytics import (
    StatusCount,
    StatusDistribution,
    VolumeBucket,
    VolumeSeries,
)
from app.services import analytics_service

router = APIRouter(tags=["analytics"])

_analyst = require_role(Role.tenant_admin, Role.system_admin)


@router.get("/analytics/volume", response_model=VolumeSeries)
async def incident_volume(
    # `from` is a Python keyword, so it can only be reached through an alias — the same
    # pattern as `status_filter` in routes/incidents.py. `to` is aliased for symmetry.
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    severity: Severity | None = Query(default=None),
    tenant_id: uuid.UUID | None = Query(default=None),
    actor: CurrentUser = Depends(_analyst),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> VolumeSeries:
    """UC-10 steps 2-3, 5: incidents created per week over the requested window.

    The zone is resolved before the window, because "this week" depends on it: run at
    09:00 Monday in Melbourne it is 06:00 Monday in Perth and 22:00 Sunday in UTC, so the
    default window's last bucket differs. The resolved zone is echoed in `timezone` so the
    frontend labels the bars with the same Mondays the server bucketed by.
    """
    tz_name = await analytics_service.resolve_timezone(
        db, scope=scope, target_tenant_id=tenant_id
    )
    week_from, week_to = analytics_service.resolve_window(from_date, to_date, tz_name)
    buckets = await analytics_service.incident_volume(
        db,
        scope=scope,
        target_tenant_id=tenant_id,
        severity=severity,
        week_from=week_from,
        week_to=week_to,
        tz_name=tz_name,
    )
    return VolumeSeries(
        data=[VolumeBucket(week_start=day, count=count) for day, count in buckets],
        timezone=tz_name,
        from_=week_from,
        to=week_to,
        severity=severity,
    )


@router.get("/analytics/status-distribution", response_model=StatusDistribution)
async def status_distribution(
    tenant_id: uuid.UUID | None = Query(default=None),
    actor: CurrentUser = Depends(_analyst),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> StatusDistribution:
    """UC-10 step 4: how incidents are distributed across the workflow right now."""
    counts = await analytics_service.status_distribution(
        db, scope=scope, target_tenant_id=tenant_id
    )
    return StatusDistribution(
        data=[
            StatusCount(status=state, count=count) for state, count in counts.items()
        ],
        total=sum(counts.values()),
    )
