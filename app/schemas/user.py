"""User management schemas (UC-03, US-03)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import Role, UserStatus


class UserInvite(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    role: Role


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: Role
    status: UserStatus
    created_at: datetime
