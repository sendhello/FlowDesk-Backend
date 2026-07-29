"""Notifications: dispatch and panel (UC-09; US-13, US-14).

Two halves of one domain noun, in one module (the same layout as incident_service, which
holds create + list + get):

    dispatch  -- notify_incident_transitioned / notify_incident_reassigned, called by
                 workflow_service BEFORE its commit, on the same session.
    panel     -- list_notifications / unread_count / mark_read / mark_all_read.

The two hook signatures are FROZEN. workflow_service calls them with exactly these
keyword arguments and tests/test_workflow.py asserts on `kwargs["from_status"]`,
`["to_status"]` and `["actor"]`. Callers must import the MODULE
(`from app.services import notification_service`) rather than the functions, so tests can
substitute a spy with monkeypatch.setattr.

UC-09 E1 vs atomicity
---------------------
E1: "System logs the error. The user can still view the incident through the incident
list." The hook runs inside the transition's transaction, so a failing INSERT would roll
the state change back with it. Two things prevent that, in order:

1. `_deliver` pre-checks the recipient. A deleted user is a foreign-key violation and an
   inactive one is an unreachable row; one cheap SELECT turns both into a logged no-op.
2. The insert itself runs inside a SAVEPOINT. If it fails anyway, only the savepoint is
   rolled back and the caller's transaction stays usable.

A savepoint, not a second transaction after the commit: a second transaction leaves a
window where the transition is durable and the notification is lost forever. This way a
notification that succeeds is still atomic with the state change, and only one that fails
is dropped -- best-effort exactly where E1 asks for it and nowhere else.

Note `try/except` around a bare `db.add()` would NOT work: `add()` only stages the object,
the failure surfaces at flush/commit, and by then the transaction is aborted and even the
commit fails. The explicit `flush()` inside `begin_nested()` is what makes the guard real.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.logging import log_notification_skipped
from app.models.enums import IncidentStatus, UserStatus
from app.models.incident import Incident
from app.models.notification import Notification
from app.models.user import User

if TYPE_CHECKING:  # pragma: no cover
    from app.api.deps import CurrentUser

# Read from the column rather than hardcoded, so the two can never disagree.
MESSAGE_MAX_LENGTH: int = Notification.__table__.c.message.type.length

# Derived from the enum, so a new state cannot be missed: "Open" / "In Review" / "Closed".
_STATE_LABELS: dict[IncidentStatus, str] = {
    state: state.value.replace("_", " ").title() for state in IncidentStatus
}

# UC-09 step 2: the message carries the incident title and the new state.
_TRANSITIONED_TEMPLATE = '"{title}" is now {state}.'
_REASSIGNED_TEMPLATE = 'You have been assigned "{title}" ({state}).'


def _render(template: str, *, title: str, state: IncidentStatus) -> str:
    """Render a message that PROVABLY fits `notifications.message` (String(500)).

    Titles are String(255) and the longest template is ~40 characters, so the truncation
    branch is unreachable today -- that is the point: it is provably safe rather than
    accidentally safe. A longer template or a schema change would otherwise raise
    StringDataRightTruncation at the commit, inside the transition's transaction.
    """
    label = _STATE_LABELS[state]
    budget = MESSAGE_MAX_LENGTH - len(template.format(title="", state=label))
    if len(title) > budget:
        title = title[: max(budget - 1, 0)] + "…"  # one character, not three dots
    return template.format(title=title, state=label)[:MESSAGE_MAX_LENGTH]


async def _deliver(
    db: AsyncSession,
    *,
    incident: Incident,
    actor: "CurrentUser",
    recipient_id: uuid.UUID,
    message: str,
) -> None:
    """Stage one notification row on the caller's session. Never commits.

    The caller's commit makes it durable, so a successful notification is atomic with the
    state change that caused it.
    """
    if recipient_id == actor.id:
        # Nobody needs a bell badge for the button they just clicked. Reachable today:
        # `submitted_by` is a historical fact, but a caller's role is resolved from the DB
        # on every request, so a staff member promoted to reviewer can transition their
        # own earlier submission. The guard lives here rather than in either hook so no
        # future RBAC change can reintroduce it.
        return

    recipient = await db.scalar(select(User).where(User.id == recipient_id))
    if recipient is None or recipient.status is not UserStatus.active:
        # UC-09 E1. Inserting anyway would either violate the users FK or create a row
        # nobody can ever reach (an inactive user is refused at get_current_user), and the
        # FK violation would roll the caller's state change back with it.
        log_notification_skipped(
            reason="recipient_missing" if recipient is None else "recipient_inactive",
            incident_id=str(incident.id),
            recipient_id=str(recipient_id),
        )
        return

    # `begin_nested()` unconditionally flushes pending state BEFORE emitting SAVEPOINT
    # (documented SQLAlchemy behaviour, independent of autoflush). That is what makes this
    # safe: the caller's own pending rows -- the WorkflowTransition staged by
    # workflow_service -- are written to the OUTER transaction, so a savepoint rollback
    # here discards only the notification and never the timeline entry.
    try:
        async with db.begin_nested():  # SAVEPOINT
            db.add(
                Notification(
                    tenant_id=incident.tenant_id,
                    user_id=recipient_id,
                    incident_id=incident.id,
                    message=message,
                )
            )
            await db.flush()  # force the failure INSIDE the savepoint
    except SQLAlchemyError:  # ROLLBACK TO SAVEPOINT -- the session stays usable
        log_notification_skipped(
            reason="insert_failed",
            incident_id=str(incident.id),
            recipient_id=str(recipient_id),
        )


# ---- Dispatch (US-13) -----------------------------------------------------------


async def notify_incident_transitioned(
    db: AsyncSession,
    *,
    incident: Incident,
    actor: "CurrentUser",
    from_status: IncidentStatus,
    to_status: IncidentStatus,
) -> None:
    """UC-08 step 9 / UC-09 steps 1-2. Notify the SUBMITTER.

    Only a real state change reaches this hook -- replays and rejected transitions do not.

    `incident` was loaded BEFORE workflow_service's guarded Core UPDATE, so
    `incident.status` is STALE: it still holds `from_status`. Always use the `to_status`
    argument. `id`, `tenant_id`, `title` and `submitted_by` are unaffected.

    UC-08 step 9 names the submitter; UC-09's precondition additionally names the assigned
    reviewer. Shipping the narrower reading -- appending `incident.assigned_to` to
    `recipients` is the whole change if the product owner confirms the wider one, and
    `_deliver` already skips the actor and unavailable recipients.
    """
    recipients = [incident.submitted_by]
    message = _render(_TRANSITIONED_TEMPLATE, title=incident.title, state=to_status)
    for recipient_id in dict.fromkeys(r for r in recipients if r is not None):
        await _deliver(
            db, incident=incident, actor=actor, recipient_id=recipient_id, message=message
        )


async def notify_incident_reassigned(
    db: AsyncSession,
    *,
    incident: Incident,
    actor: "CurrentUser",
    previous_assignee_id: uuid.UUID | None,
    new_assignee_id: uuid.UUID,
) -> None:
    """UC-08 A1.2. Notify the NEW reviewer.

    The status does not change, so the message carries the incident's current state for
    context. Here `incident.status` IS accurate: reassign() mutates the ORM object rather
    than issuing a Core UPDATE.

    The previous assignee is not notified: the SRS does not ask for it, and it would tell
    someone their queue shrank without telling them anything actionable.
    """
    message = _render(
        _REASSIGNED_TEMPLATE, title=incident.title, state=incident.status
    )
    await _deliver(
        db, incident=incident, actor=actor, recipient_id=new_assignee_id, message=message
    )


# ---- Panel (US-14) --------------------------------------------------------------


def _recipient_conds(actor: "CurrentUser") -> list:
    """THE definition of "which notifications this caller may see" (UC-09).

    Used by the list, the unread count, mark-one-read and mark-all-read, so a 404 from
    mark-read can never drift from an omission from the list -- the same single-predicate
    discipline as incident_service._visibility_conds.

    Deliberately NOT role-aware and NOT widened for system_admin: a notification is
    personal correspondence, not tenant data. `tenant_id` is intentionally absent too --
    `user_id` already implies the tenant, and a redundant predicate would be a second,
    subtly different definition of visibility.
    """
    return [Notification.user_id == actor.id]


async def list_notifications(
    db: AsyncSession,
    *,
    actor: "CurrentUser",
    is_read: bool | None,
    limit: int,
    offset: int,
) -> tuple[list[Notification], int]:
    """UC-09 step 5: the caller's notifications, newest first.

    The order is FIXED rather than caller-selectable -- UC-09 specifies exactly one. `id`
    is the deterministic tiebreak that keeps pagination stable when rows share a
    `created_at` (identical reasoning to incident_service.list_incidents).
    """
    conds = _recipient_conds(actor)
    if is_read is not None:
        conds.append(Notification.is_read.is_(is_read))

    total = await db.scalar(select(func.count()).select_from(Notification).where(*conds))
    rows = (
        await db.scalars(
            select(Notification)
            .where(*conds)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def unread_count(db: AsyncSession, *, actor: "CurrentUser") -> int:
    """UC-09 step 3: the bell badge.

    Built from the same predicate as the list, so `GET /notifications?is_read=false`
    and this endpoint agree by construction rather than by convention.
    """
    total = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(*_recipient_conds(actor), Notification.is_read.is_(False))
    )
    return int(total or 0)


async def mark_read(
    db: AsyncSession, *, actor: "CurrentUser", notification_id: uuid.UUID
) -> Notification:
    """UC-09 steps 6-7.

    Idempotent by postcondition (the NFR-06 reasoning): a second call finds the row
    already read, writes nothing and does not even commit.

    A notification that does not exist, belongs to another user, or belongs to another
    tenant all raise the identical 404 -- existence is not leaked (NFR-12).
    """
    notification = await db.scalar(
        select(Notification).where(
            Notification.id == notification_id, *_recipient_conds(actor)
        )
    )
    if notification is None:
        raise NotFoundError(
            "Notification not found.", details={"reason": "notification_not_found"}
        )
    if not notification.is_read:
        notification.is_read = True
        await db.commit()
    return notification


async def mark_all_read(db: AsyncSession, *, actor: "CurrentUser") -> int:
    """UC-09 A1: dismiss all. Returns how many rows were actually flipped.

    "Dismiss" means "mark read", not "delete": there is no `deleted_at` column, and UC-09's
    postcondition requires notifications to remain accessible in the panel.

    `synchronize_session=False` is safe and cheaper here -- nothing in the session is
    reused after this, and asyncpg reports a reliable rowcount from the command tag.
    """
    result = await db.execute(
        update(Notification)
        .where(*_recipient_conds(actor), Notification.is_read.is_(False))
        .values(is_read=True)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return int(result.rowcount or 0)
