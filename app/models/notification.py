"""Notification model (UC-09; US-13, US-14).

`tenant_id` is added here (Part 3A.4) so every tenant-scoped table is uniform for NFR-12,
resolving the SRS prose-vs-diagram discrepancy in favour of the prose. It is written from
the incident but never used as a query predicate: `user_id` already implies the tenant
(see notification_service._recipient_conds).

The composite indexes below MUST stay identical to migration
`0003_notification_query_indexes`: `alembic/env.py` diffs against `Base.metadata`, and
`tests/conftest.py` builds the test schema with `metadata.create_all` rather than by
running migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        # No `index=True`: migration 0003 drops the single-column ix_notifications_user_id
        # as a strict prefix of both composites in __table_args__. Leaving it here would
        # make `alembic check` demand its recreation on every run.
    )
    incident_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Panel ordering (UC-09 step 5). Plain ASC on purpose: the sort is
        # `created_at DESC, id DESC` — one direction throughout — so PostgreSQL scans this
        # btree backwards with no sort step. A DESC index only helps mixed-direction
        # orders and would risk `alembic check` noise for nothing.
        Index("ix_notifications_user_created_at", "user_id", "created_at"),
        # Serves the unread badge count, the ?is_read= panel filter and the read-all
        # UPDATE, all of which are (user_id, is_read).
        Index("ix_notifications_user_is_read", "user_id", "is_read"),
    )
