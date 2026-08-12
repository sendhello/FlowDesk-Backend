"""Incident submission, listing and detail (UC-06, UC-07, UC-11; US-08, US-09, US-12).

Service functions take duck-typed `actor`/`scope` objects (deps.CurrentUser /
deps.TenantScope), imported only under TYPE_CHECKING to avoid a circular import with
app.api.deps — the same pattern as user_service.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.exceptions import NotFoundError, ValidationError
from app.models.category import Category
from app.models.enums import IncidentStatus, Role, Severity
from app.models.incident import Incident
from app.models.workflow_transition import WorkflowTransition
from app.schemas.common import SortOrder
from app.schemas.incident import IncidentSort

if TYPE_CHECKING:  # pragma: no cover
    from app.api.deps import CurrentUser, TenantScope

_SORT_COLUMNS = {
    IncidentSort.created_at: Incident.created_at,
    IncidentSort.updated_at: Incident.updated_at,
    IncidentSort.title: Incident.title,
    # `severity` and `status` are native PG enums whose declaration order is already the
    # semantic order (low->critical, open->closed), so ORDER BY on them is meaningful.
    IncidentSort.severity: Incident.severity,
    IncidentSort.status: Incident.status,
}


def _row_options() -> tuple:
    """Eager loads for a list row. Relationships are lazy="raise", so these are required."""
    return (
        joinedload(Incident.category),
        joinedload(Incident.submitter),
        joinedload(Incident.assignee),
    )


def _detail_options() -> tuple:
    """Eager loads for the detail payload: a row plus the ordered timeline (UC-11)."""
    return _row_options() + (
        selectinload(Incident.transitions).joinedload(WorkflowTransition.actor),
    )


def _visibility_conds(actor: "CurrentUser", scope: "TenantScope") -> list:
    """THE definition of "which incidents this caller may see" (US-09 + UC-11).

    Used by both the list query and the single-row loaders, so a 404 on the detail
    endpoint and an omission from the list can never drift apart.

        system_admin -> every tenant (optionally pinned by ?tenant_id=)
        tenant_admin -> own tenant
        reviewer     -> own tenant, assigned to them OR unassigned
        staff        -> own tenant, submitted by them

    The reviewer's "OR unassigned" arm is deliberate. UC-07 as written scopes reviewers to
    `assigned_to == me`, but UC-06 creates incidents with no assignee — so no reviewer
    could ever see a new incident and the workflow would be unreachable. Reviewers work
    from a shared queue instead, and claim an incident by moving it to In Review
    (see workflow_service). Pending confirmation from the product owner.
    """
    if scope.is_system_admin:
        return []
    conds = [Incident.tenant_id == scope.tenant_id]
    if actor.role is Role.reviewer:
        conds.append(
            or_(Incident.assigned_to == actor.id, Incident.assigned_to.is_(None))
        )
    elif actor.role is Role.staff:
        conds.append(Incident.submitted_by == actor.id)
    return conds


async def create_incident(
    db: AsyncSession,
    *,
    actor: "CurrentUser",
    category_id: uuid.UUID,
    title: str,
    description: str,
    severity: Severity,
) -> Incident:
    """UC-06 main flow. Status, submitter and tenant are set by the server, never by the
    request body."""
    # UC-06 step 4. Not reusing category_service.get_category: it raises 404, but the
    # category here is a *body field*, not the addressed resource — a 404 on
    # POST /incidents reads to the frontend as "this endpoint does not exist".
    category = await db.scalar(
        select(Category).where(
            Category.id == category_id, Category.tenant_id == actor.tenant_id
        )
    )
    if category is None:
        # One message for "no such category" and "category of another tenant", so
        # existence is not leaked (NFR-12).
        raise ValidationError(
            "The selected category does not exist in your organisation.",
            details={"reason": "category_not_in_tenant"},
        )

    incident = Incident(
        tenant_id=actor.tenant_id,
        category_id=category_id,
        submitted_by=actor.id,
        title=title,
        description=description,
        severity=severity,
        status=IncidentStatus.open,
    )
    db.add(incident)
    await db.commit()
    return await _load(db, incident_id=incident.id, options=_detail_options())


async def list_incidents(
    db: AsyncSession,
    *,
    actor: "CurrentUser",
    scope: "TenantScope",
    target_tenant_id: uuid.UUID | None,
    status: IncidentStatus | None,
    severity: Severity | None,
    sort: IncidentSort,
    order: SortOrder,
    limit: int,
    offset: int,
) -> tuple[list[Incident], int]:
    """UC-07. Role-scoped, filtered, sorted and paginated."""
    conds = _visibility_conds(actor, scope)
    if scope.is_system_admin and target_tenant_id is not None:
        conds.append(Incident.tenant_id == target_tenant_id)
    if status is not None:
        conds.append(Incident.status == status)
    if severity is not None:
        conds.append(Incident.severity == severity)

    column = _SORT_COLUMNS[sort]
    direction = column.asc() if order is SortOrder.asc else column.desc()
    # `id` is a deterministic tiebreak: without it, rows tying on the sort key can be
    # returned in a different order per page, duplicating or skipping rows across pages.
    tiebreak = Incident.id.asc() if order is SortOrder.asc else Incident.id.desc()

    total = await db.scalar(select(func.count()).select_from(Incident).where(*conds))
    rows = (
        await db.scalars(
            select(Incident)
            .where(*conds)
            .options(*_row_options())
            .order_by(direction, tiebreak)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def _load(db: AsyncSession, *, incident_id: uuid.UUID, options: tuple) -> Incident:
    """Load one incident with the given eager-load options, ignoring visibility.

    `populate_existing` is essential, not cosmetic: the session runs with
    `expire_on_commit=False`, so a just-inserted or just-updated incident is still in the
    identity map and a plain SELECT would hand back that same instance without applying
    these eager loads — and every relationship here is `lazy="raise"`.
    """
    incident = await db.scalar(
        select(Incident)
        .where(Incident.id == incident_id)
        .options(*options)
        .execution_options(populate_existing=True)
    )
    if incident is None:
        raise NotFoundError(
            "Incident not found.", details={"reason": "incident_not_found"}
        )
    return incident


async def get_incident(
    db: AsyncSession,
    *,
    actor: "CurrentUser",
    scope: "TenantScope",
    incident_id: uuid.UUID,
    with_history: bool = False,
) -> Incident:
    """Load one visible incident (UC-11 step 1-3).

    UC-11 E1: out of scope raises NotFoundError -> 404, never 403. Existence is not
    leaked (see tests/test_tenant_isolation.py).
    """
    options = _detail_options() if with_history else _row_options()
    incident = await db.scalar(
        select(Incident)
        .where(Incident.id == incident_id, *_visibility_conds(actor, scope))
        .options(*options)
        .execution_options(populate_existing=True)  # see _load
    )
    if incident is None:
        # Same slug as the internal re-read above, deliberately. The two messages differ
        # and the contract used to make a client handle both strings; one slug is the
        # whole point of D-5.
        raise NotFoundError(
            "Incident not found or you do not have permission to view it.",
            details={"reason": "incident_not_found"},
        )
    return incident
