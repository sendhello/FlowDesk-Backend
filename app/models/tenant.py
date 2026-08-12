"""Tenant (organisation) model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: Timezone a tenant starts with, and the value migration 0004 backfilled onto every row
#: that predates the column. Matches `REPORTING_TIMEZONE` in fly.toml, so the settings
#: screen shows the same zone the analytics were already being computed in — introducing
#: the column must not silently move anybody's weekly buckets.
DEFAULT_TENANT_TIMEZONE = "Australia/Melbourne"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    #: IANA zone name. NOT NULL rather than "NULL means the platform default", so the
    #: settings screen always has something to render and there is one fewer state to
    #: reason about. Validated in settings_service, not here — see that module.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=DEFAULT_TENANT_TIMEZONE
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
