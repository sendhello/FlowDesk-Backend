"""Workflow transition (audit trail of incident state changes).

Not tenant-scoped directly: every transition is reachable only via its parent incident,
which carries `tenant_id`. The service layer always loads the incident and asserts its
`tenant_id` before touching transitions, so cross-tenant access is impossible
(Part 3A.4). Schema now; logic in Sprint 2 (UC-08)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import IncidentStatus, incident_status_enum


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
