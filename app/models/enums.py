"""Domain enumerations, shared by the ORM models and the Alembic migration.

Native PostgreSQL enum types are used. Type names here MUST match the names created in
the initial migration (`0001_initial_schema.py`).
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """RBAC roles (SRS §1.2, four roles)."""

    system_admin = "system_admin"
    tenant_admin = "tenant_admin"
    staff = "staff"
    reviewer = "reviewer"


class UserStatus(str, Enum):
    active = "active"
    inactive = "inactive"


class Severity(str, Enum):
    """Incident severity (UC-06)."""

    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class IncidentStatus(str, Enum):
    """Workflow states (SRS §1.2: Open -> In Review -> Closed)."""

    open = "open"
    in_review = "in_review"
    closed = "closed"


# PostgreSQL enum type names (single source of truth for models + migration).
ROLE_ENUM_NAME = "role"
USER_STATUS_ENUM_NAME = "user_status"
SEVERITY_ENUM_NAME = "severity"
INCIDENT_STATUS_ENUM_NAME = "incident_status"


def _values(enum_cls: type[Enum]) -> list[str]:
    """Store the enum *values* (e.g. "system_admin"), not the member names."""
    return [member.value for member in enum_cls]


# Shared SQLAlchemy type instances. Reusing one instance per PostgreSQL enum type
# (in particular `incident_status_enum` across incident.status, from_status, to_status)
# means the type is defined once. `metadata.create_all(checkfirst=True)` dedups DDL.
from sqlalchemy import Enum as SAEnum  # noqa: E402

role_enum = SAEnum(Role, name=ROLE_ENUM_NAME, values_callable=_values, native_enum=True)
user_status_enum = SAEnum(
    UserStatus, name=USER_STATUS_ENUM_NAME, values_callable=_values, native_enum=True
)
severity_enum = SAEnum(
    Severity, name=SEVERITY_ENUM_NAME, values_callable=_values, native_enum=True
)
incident_status_enum = SAEnum(
    IncidentStatus,
    name=INCIDENT_STATUS_ENUM_NAME,
    values_callable=_values,
    native_enum=True,
)
