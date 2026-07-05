"""Declarative base and metadata.

Importing this module (and, transitively, all models) gives Alembic a single
`Base.metadata` describing every table for autogenerate and for `create_all` in tests.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""


# Import all models so they register on Base.metadata. Kept at the bottom to avoid
# circular imports; `noqa` because these are imported for their side effects.
from app.models import (  # noqa: E402,F401
    category,
    incident,
    notification,
    tenant,
    user,
    workflow_transition,
)
