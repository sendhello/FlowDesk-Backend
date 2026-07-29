"""Incident model (UC-06, UC-07, UC-11; US-08..US-12).

Relationships are declared `lazy="raise"` deliberately. Under async SQLAlchemy a
forgotten eager-load otherwise surfaces as `MissingGreenlet` during response
serialisation — in production. `raise` turns that into a loud, deterministic failure in
the test suite instead.

The composite indexes below MUST stay identical to migration `0002_incident_query_indexes`:
`alembic/env.py` diffs against `Base.metadata`, and `tests/conftest.py` builds the test
schema with `metadata.create_all` rather than by running migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    IncidentStatus,
    Severity,
    incident_status_enum,
    severity_enum,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.models.category import Category
    from app.models.user import User
    from app.models.workflow_transition import WorkflowTransition


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("categories.id"), nullable=False
    )
    submitted_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[Severity] = mapped_column(severity_enum, nullable=False)
    status: Mapped[IncidentStatus] = mapped_column(
        incident_status_enum, nullable=False, default=IncidentStatus.open
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    category: Mapped["Category"] = relationship(lazy="raise")
    # Two FKs point at `users`, so `foreign_keys=` is mandatory to disambiguate.
    submitter: Mapped["User"] = relationship(foreign_keys=[submitted_by], lazy="raise")
    assignee: Mapped["User | None"] = relationship(
        foreign_keys=[assigned_to], lazy="raise"
    )
    transitions: Mapped[list["WorkflowTransition"]] = relationship(
        back_populates="incident",
        # `created_at` defaults to now(), which in PostgreSQL is *transaction* start
        # time. Each transition is its own request and therefore its own transaction, so
        # the timestamps strictly increase; `id` is a deterministic tiebreak regardless.
        order_by="WorkflowTransition.created_at, WorkflowTransition.id",
        lazy="raise",
        passive_deletes=True,  # the FK's ON DELETE CASCADE already does the work
    )

    __table_args__ = (
        # PostgreSQL does not index foreign keys automatically. Every role-scoped list
        # in US-09 filters on one of these alongside tenant_id (NFR-01, NFR-03).
        Index("ix_incidents_tenant_status", "tenant_id", "status"),
        Index("ix_incidents_tenant_submitted_by", "tenant_id", "submitted_by"),
        Index("ix_incidents_tenant_assigned_to", "tenant_id", "assigned_to"),
        Index("ix_incidents_tenant_created_at", "tenant_id", "created_at"),
        Index("ix_incidents_category_id", "category_id"),
    )
