"""Incident workflow state machine (UC-08; US-10, US-11).

The only legal edges are open -> in_review -> closed. Everything else about the machine
(which states are reachable, which requests are idempotent replays) is DERIVED from
`_ALLOWED`, so adding an edge is a one-line change.

NFR-06 vs UC-08 E1 -- the two requirements appear to conflict, because a repeat of a
successful `open -> in_review` reads literally as `in_review -> in_review`, which is not a
legal edge. The resolution:

    Idempotency is defined over the POSTCONDITION, not over the request.

A request is a REPLAY when the incident already occupies the requested target state AND
that state is reachable by some legal edge. It returns 200 with the current state and
writes nothing -- the postcondition the caller asked for holds, so nothing untrue is
reported. A request is UC-08 E1 when the target is neither the current state nor reachable
from it, OR when the target is `open`: `open` is the initial state and is never the target
of a legal edge, so `open -> open` is not a replay of anything. E1 is itself idempotent, so
NFR-06 still holds.

Corollary: a replayed close does NOT re-validate the resolution note. E2 guards the
*writing of a row*; on a replay no row is written, so there is nothing to guard.

NOTE: this reasoning holds only while the machine is acyclic. Adding a reopen edge
(closed -> open) would make a legitimate second visit to a state indistinguishable from a
duplicate request, and NFR-06 would then need a request-scoped idempotency key.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import log_privileged_action, log_workflow_transition
from app.models.enums import IncidentStatus, Role, UserStatus
from app.models.incident import Incident
from app.models.user import User
from app.models.workflow_transition import WorkflowTransition
from app.services import incident_service, notification_service

if TYPE_CHECKING:  # pragma: no cover
    from app.api.deps import CurrentUser, TenantScope

# The single source of truth for the machine. UC-08: Open -> In Review -> Closed.
_ALLOWED: frozenset[tuple[IncidentStatus, IncidentStatus]] = frozenset(
    {
        (IncidentStatus.open, IncidentStatus.in_review),
        (IncidentStatus.in_review, IncidentStatus.closed),
    }
)
# States that some legal edge leads to. Exactly the states for which `from == to` is a
# replay rather than UC-08 E1. Derived, never hand-written.
_REACHABLE: frozenset[IncidentStatus] = frozenset(to for _, to in _ALLOWED)
# UC-08 E2: closing requires a resolution note.
_REQUIRES_NOTE: frozenset[IncidentStatus] = frozenset({IncidentStatus.closed})

# UC-08 names Reviewer as the actor. Tenant Admin is included because they already have
# full read over the tenant's incidents and are its escape hatch (UC-03, UC-04) -- without
# them an incident is stuck if its reviewer is deactivated, since the MVP has no unassign
# endpoint. System Admin is a platform actor, not a business actor, and is excluded.
# Pending confirmation from the product owner.
_TRANSITION_ROLES: frozenset[Role] = frozenset({Role.reviewer, Role.tenant_admin})

# Verbatim from the SRS (UC-08 E1.1 / E2.1) so the traceability matrix is literal.
_E1_MESSAGE = "This transition is not permitted from the current state."
_E2_MESSAGE = "A resolution note is required to close this incident."


def allowed_transitions(status: IncidentStatus, role: Role) -> list[IncidentStatus]:
    """States this role may move an incident in `status` to (UC-08 steps 1-3, UC-11 4).

    Returned to the frontend so it renders the right action buttons without
    re-implementing the machine. Empty for staff and for a closed incident.
    """
    if role not in _TRANSITION_ROLES:
        return []
    targets = {to for frm, to in _ALLOWED if frm is status}
    # Iterate the enum for a stable, declaration-ordered result.
    return [state for state in IncidentStatus if state in targets]


def _invalid_transition(from_status: IncidentStatus, to_status: IncidentStatus) -> ConflictError:
    """UC-08 E1. 409, not 422: the body is well-formed, the resource *state* is wrong."""
    return ConflictError(
        _E1_MESSAGE,
        details={
            "reason": "invalid_transition",
            "from_status": from_status.value,
            "to_status": to_status.value,
            "allowed": [to.value for frm, to in _ALLOWED if frm is from_status],
        },
    )


async def transition(
    db: AsyncSession,
    *,
    actor: "CurrentUser",
    scope: "TenantScope",
    incident_id: uuid.UUID,
    to_status: IncidentStatus,
    note: str | None,
) -> Incident:
    """Apply a workflow transition (UC-08 main flow), or recognise an idempotent replay.

    Raises NotFoundError when the incident is out of scope, ConflictError
    (`invalid_transition`, UC-08 E1) and ValidationError (`resolution_note_required`,
    UC-08 E2).
    """
    incident = await incident_service.get_incident(
        db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
    )
    from_status = incident.status

    if to_status is from_status:
        if to_status not in _REACHABLE:
            raise _invalid_transition(from_status, to_status)
        return incident  # replay: nothing written, nothing logged, no hook fired

    if (from_status, to_status) not in _ALLOWED:
        raise _invalid_transition(from_status, to_status)

    if to_status in _REQUIRES_NOTE and not (note or "").strip():
        raise ValidationError(
            _E2_MESSAGE, details={"reason": "resolution_note_required"}
        )

    values: dict = {"status": to_status}
    # A reviewer claims an unassigned incident by moving it to In Review. Without this,
    # UC-07's `assigned_to == me` scope plus UC-06's unassigned creation would leave every
    # new incident invisible to every reviewer. See incident_service._visibility_conds.
    claimed = (
        from_status is IncidentStatus.open
        and to_status is IncidentStatus.in_review
        and incident.assigned_to is None
    )
    if claimed:
        values["assigned_to"] = actor.id

    # Guarded UPDATE: the WHERE clause carries the expected current state, so two
    # concurrent identical requests cannot both write a transition row (NFR-06). No row
    # lock is held, so this behaves the same under a session or transaction-mode pooler.
    won = (
        await db.execute(
            update(Incident)
            .where(Incident.id == incident_id, Incident.status == from_status)
            .values(**values)
            .returning(Incident.id)
        )
    ).scalar_one_or_none() is not None

    if not won:
        # Someone else moved the incident between our read and our write. Re-read and
        # re-classify against the state that actually won.
        await db.rollback()
        fresh = await incident_service.get_incident(
            db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
        )
        if fresh.status is to_status and to_status in _REACHABLE:
            return fresh
        raise _invalid_transition(fresh.status, to_status)

    db.add(
        WorkflowTransition(
            incident_id=incident_id,
            from_status=from_status,
            to_status=to_status,
            transitioned_by=actor.id,
            note=note,
        )
    )
    # Before the commit and on the same session, so Sprint 3 (US-13) gets atomicity with
    # the state change for free.
    await notification_service.notify_incident_transitioned(
        db,
        incident=incident,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
    )
    await db.commit()

    # After the commit, so the audit line only ever describes a durable fact (NFR-07).
    log_workflow_transition(
        incident_id=str(incident_id),
        actor_id=str(actor.id),
        from_status=from_status.value,
        to_status=to_status.value,
        claimed=claimed,
    )
    return await incident_service.get_incident(
        db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
    )


async def reassign(
    db: AsyncSession,
    *,
    actor: "CurrentUser",
    scope: "TenantScope",
    incident_id: uuid.UUID,
    assigned_to: uuid.UUID,
) -> Incident:
    """Reassign an incident to another reviewer (UC-08 A1).

    Writes NO workflow_transitions row: `from_status`/`to_status` are NOT NULL there, and
    a degenerate from == to row would corrupt both the timeline and the
    idempotency-by-state reasoning above. Audited via log_privileged_action instead.
    """
    incident = await incident_service.get_incident(
        db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
    )
    if incident.status is IncidentStatus.closed:
        raise ConflictError(
            "A closed incident cannot be reassigned.",
            details={"reason": "incident_closed"},
        )

    # The target must belong to the INCIDENT's tenant, not the actor's: a System Admin
    # acting inside tenant B must pick a reviewer from tenant B.
    target = await db.scalar(
        select(User).where(
            User.id == assigned_to,
            User.tenant_id == incident.tenant_id,
            User.role == Role.reviewer,
            User.status == UserStatus.active,
        )
    )
    if target is None:
        # One message covers "no such user", "wrong tenant" and "wrong role", so nothing
        # about other tenants is leaked (NFR-12).
        raise ValidationError(
            "The selected user is not an active reviewer in this organisation.",
            details={"reason": "invalid_assignee"},
        )

    previous_assignee_id = incident.assigned_to
    incident.assigned_to = target.id
    await notification_service.notify_incident_reassigned(
        db,
        incident=incident,
        actor=actor,
        previous_assignee_id=previous_assignee_id,
        new_assignee_id=target.id,
    )
    await db.commit()

    log_privileged_action(
        "incident_reassign",
        actor_id=str(actor.id),
        incident_id=str(incident_id),
        previous_assignee=str(previous_assignee_id) if previous_assignee_id else None,
        new_assignee=str(target.id),
    )
    return await incident_service.get_incident(
        db, actor=actor, scope=scope, incident_id=incident_id, with_history=True
    )
