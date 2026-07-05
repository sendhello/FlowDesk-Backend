"""Organisation registration schemas (UC-02, US-01)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import Role, UserStatus


class OrgRegisterRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=255)
    admin_email: EmailStr
    admin_name: str = Field(min_length=1, max_length=255)


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: Role
    status: UserStatus


class OrgRegisterResponse(BaseModel):
    tenant: TenantOut
    admin_user: AdminUserOut
