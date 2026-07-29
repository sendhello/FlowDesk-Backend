"""Workflow transition (audit trail of incident state changes).

Not tenant-scoped directly: every transition is reachable only via its parent incident,
which carries `tenant_id`. The service layer always loads the incident and asserts its
`tenant_id` before touching transitions, so cross-tenant access is impossible
(Part 3A.4).

`from_status` and `to_status` are NOT NULL, so this table records *state changes only*.
Reassignment (UC-08 A1) has no state change and therefore writes no row here — a
degenerate from == to row would corrupt both the UI timeline and the idempotency-by-state
reasoning in workflow_service. Reassignment is audited via `log_privileged_action`
instead. For the same reason a freshly-submitted incident has an empty timeline: the
frontend renders the creation entry from the incident's own `created_at` / `submitted_by`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import IncidentStatus, incident_status_enum

if TYPE_CHECKING:  # pragma: no cover
    from app.models.incident import Incident
    from app.models.user import User


class WorkflowTransition(Base):
    __tablename__ = "workflow_transitions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("incidents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_status: Mapped[IncidentStatus] = mapped_column(
        incident_status_enum, nullable=False
    )
    to_status: Mapped[IncidentStatus] = mapped_column(
        incident_status_enum, nullable=False
    )
    transitioned_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    incident: Mapped["Incident"] = relationship(
        back_populates="transitions", lazy="raise"
    )
    actor: Mapped["User"] = relationship(
        foreign_keys=[transitioned_by], lazy="raise"
    )
