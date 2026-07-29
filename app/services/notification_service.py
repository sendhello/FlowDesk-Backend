"""Notification dispatch (UC-09; US-13, US-14 — Sprint 3).

Sprint 2 wires the CALL SITE only, so UC-08's postcondition ("a notification is sent to
the submitter") has a connection point without the notifications feature existing. The
`notifications` table is already in migration 0001, but writing rows to it is US-13 and
belongs to Sprint 3 — a stub that does nothing is more honest than a half-built feature.

The hooks receive the caller's AsyncSession and are invoked BEFORE the commit, so Sprint 3
can simply `db.add(Notification(...))` here and get atomicity with the state change for
free — no second transaction, no dual-write problem.

Callers must import the MODULE (`from app.services import notification_service`) rather
than the functions, so tests can substitute a spy with monkeypatch.setattr.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import IncidentStatus
from app.models.incident import Incident

if TYPE_CHECKING:  # pragma: no cover
    from app.api.deps import CurrentUser


async def notify_incident_transitioned(
    db: AsyncSession,
    *,
    incident: Incident,
    actor: "CurrentUser",
    from_status: IncidentStatus,
    to_status: IncidentStatus,
) -> None:
    """UC-08 step 9. Sprint 3 (US-13): notify incident.submitted_by.

    Only a real state change reaches this hook — replays and rejected transitions do not.
    """
    return None


async def notify_incident_reassigned(
    db: AsyncSession,
    *,
    incident: Incident,
    actor: "CurrentUser",
    previous_assignee_id: uuid.UUID | None,
    new_assignee_id: uuid.UUID,
) -> None:
    """UC-08 A1.2. Sprint 3 (US-13): notify the new reviewer."""
    return None
