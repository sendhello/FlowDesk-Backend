"""Auth / session schemas."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.models.enums import Role, UserStatus


class TenantRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class MeResponse(BaseModel):
    """Resolved identity for the authenticated caller (GET /me)."""

    id: uuid.UUID
    email: str
    name: str
    role: Role
    status: UserStatus
    tenant: TenantRef
