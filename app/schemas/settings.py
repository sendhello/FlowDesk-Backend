"""Tenant workspace settings schemas (the settings screen; D-18).

Scope note, because the name invites a wrong assumption. These are the settings of ONE
organisation, edited by its own Tenant Admin. They are not UC-01's *platform* settings —
that use case names System Admin as the actor and "platform name, default categories"
as the content, effective across all tenants, and none of that is built. D-19 tracks the
gap; §7.2 of the API contract states it rather than letting this endpoint imply UC-01
shipped.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TenantSettingsUpdate(BaseModel):
    """Partial update. Both columns are NOT NULL, so `null` means "leave this alone".

    No `UNSET` sentinel here, unlike `CategoryUpdate`: clearing is only meaningful for a
    nullable column, and neither of these is one. There is no way to express "this
    organisation has no name".
    """

    name: str | None = Field(default=None, min_length=2, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


class TenantSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    timezone: str
    created_at: datetime
    updated_at: datetime
