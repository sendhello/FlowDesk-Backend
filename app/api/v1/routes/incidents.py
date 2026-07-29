"""Incident and workflow routes (UC-06, UC-07, UC-08, UC-11; US-08..US-12).

Note the two-layer authorisation order: `require_role` runs before the handler body, so a
Staff user probing another tenant's incident gets 403 `insufficient_role`, not 404. That
leaks nothing — the 403 is a pure function of the caller's role and is identical for
existent and non-existent ids. Once past the role gate, anything out of the caller's
visibility scope is always 404 (see incident_service._visibility_conds).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    CurrentUser,
    PageParams,
    TenantScope,
    get_current_user,
    pagination_params,
    require_role,
    tenant_scope,
)
from app.db.session import get_db
from app.models.enums import IncidentStatus, Role, Severity
from app.models.incident import Incident
from app.schemas.common import Page, Pagination, SortOrder
from app.schemas.incident import (
    CategoryRef,
    IncidentAssign,
    IncidentCreate,
    IncidentDetail,
    IncidentOut,
    IncidentSort,
    TransitionCreate,
    TransitionOut,
    UserRef,
)
from app.services import incident_service, workflow_service

router = APIRouter(tags=["incidents"])

# UC-06 names Staff as the actor. Widening this later is additive for the frontend;
# narrowing it would be a breaking change. Pending confirmation from the product owner.
_submitter = require_role(Role.staff)
# See workflow_service._TRANSITION_ROLES for why Tenant Admin is included.
_transitioner = require_role(Role.reviewer, Role.tenant_admin)
_reassigner = require_role(Role.tenant_admin)


def _detail(incident: Incident, actor: CurrentUser) -> IncidentDetail:
    """Build the detail payload, including the caller-specific allowed transitions.

    Constructed explicitly rather than via model_validate because `allowed_transitions`
    is not an attribute of the ORM object — it is a function of state *and* role.
    """
    return IncidentDetail(
        id=incident.id,
        title=incident.title,
        description=incident.description,
        severity=incident.severity,
        status=incident.status,
        category=CategoryRef.model_validate(incident.category),
        submitted_by=UserRef.model_validate(incident.submitter),
        assigned_to=(
            UserRef.model_validate(incident.assignee) if incident.assignee else None
        ),
        created_at=incident.created_at,
        updated_at=incident.updated_at,
        transitions=[TransitionOut.model_validate(t) for t in incident.transitions],
        allowed_transitions=workflow_service.allowed_transitions(
            incident.status, actor.role
        ),
    )


@router.post(
    "/incidents", response_model=IncidentDetail, status_code=status.HTTP_201_CREATED
)
async def create_incident(
    payload: IncidentCreate,
    actor: CurrentUser = Depends(_submitter),
    db=Depends(get_db),
) -> IncidentDetail:
    """UC-06: submit an incident. Returns the detail payload so the client can render the
    incident page without a second round-trip (UC-06 step 6)."""
    incident = await incident_service.create_incident(
        db,
        actor=actor,
        category_id=payload.category_id,
        title=payload.title,
        description=payload.description,
        severity=payload.severity,
    )
    return _detail(incident, actor)


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    page: PageParams = Depends(pagination_params),
    status_filter: IncidentStatus | None = Query(default=None, alias="status"),
    severity: Severity | None = Query(default=None),
    sort: IncidentSort = Query(default=IncidentSort.created_at),
    order: SortOrder = Query(default=SortOrder.desc),
    tenant_id: uuid.UUID | None = Query(default=None),
    actor: CurrentUser = Depends(get_current_user),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> Page[IncidentOut]:
    """UC-07: incidents scoped to the caller's role and tenant."""
    rows, total = await incident_service.list_incidents(
        db,
        actor=actor,
        scope=scope,
        target_tenant_id=tenant_id,
        status=status_filter,
        severity=severity,
        sort=sort,
        order=order,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[IncidentOut](
        data=[IncidentOut.model_validate(r) for r in rows],
        pagination=Pagination(limit=page.limit, offset=page.offset, total=total),
    )


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    incident_id: uuid.UUID,
    actor: CurrentUser = Depends(get_current_user),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> IncidentDetail:
    """UC-11: full record plus the workflow timeline."""
    incident = await incident_service.get_incident(
        db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
    )
    return _detail(incident, actor)


@router.get("/incidents/{incident_id}/transitions", response_model=list[TransitionOut])
async def list_transitions(
    incident_id: uuid.UUID,
    actor: CurrentUser = Depends(get_current_user),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> list[TransitionOut]:
    """UC-11 step 3: the transition timeline on its own, same visibility rules."""
    incident = await incident_service.get_incident(
        db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
    )
    return [TransitionOut.model_validate(t) for t in incident.transitions]


@router.post("/incidents/{incident_id}/transitions", response_model=IncidentDetail)
async def transition_incident(
    incident_id: uuid.UUID,
    payload: TransitionCreate,
    actor: CurrentUser = Depends(_transitioner),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> IncidentDetail:
    """UC-08: move an incident through the workflow.

    Returns 200 + the refreshed detail (UC-08 step 10), not 201 + the transition: an
    idempotent replay returning 201 would be a lie, whereas returning the current state is
    exactly what a client means by idempotent. The method stays POST because the path is
    already frozen in the frontend contract — idempotent in effect, POST in method.
    """
    incident = await workflow_service.transition(
        db,
        actor=actor,
        scope=scope,
        incident_id=incident_id,
        to_status=payload.to_status,
        note=payload.note,
    )
    return _detail(incident, actor)


@router.post("/incidents/{incident_id}/assign", response_model=IncidentDetail)
async def assign_incident(
    incident_id: uuid.UUID,
    payload: IncidentAssign,
    actor: CurrentUser = Depends(_reassigner),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> IncidentDetail:
    """UC-08 A1: reassign the incident to another reviewer."""
    incident = await workflow_service.reassign(
        db,
        actor=actor,
        scope=scope,
        incident_id=incident_id,
        assigned_to=payload.assigned_to,
    )
    return _detail(incident, actor)
