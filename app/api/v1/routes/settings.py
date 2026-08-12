"""Tenant workspace settings routes (D-18). Scoped to the caller's tenant.

Gated on `tenant_admin` alone, matching `categories.py` — both are tenant workspace
configuration and they should not disagree about who may change it. System Admin is
excluded for the same reason it is excluded there, which is D-6 and still open: a System
Admin carries their own `tenant_id`, so letting them through would silently edit their own
organisation rather than the one they meant.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, require_role
from app.db.session import get_db
from app.models.enums import Role
from app.schemas.settings import TenantSettingsOut, TenantSettingsUpdate
from app.services import settings_service

router = APIRouter(tags=["settings"])

_tenant_admin = require_role(Role.tenant_admin)


@router.get("/settings", response_model=TenantSettingsOut)
async def read_settings(
    admin: CurrentUser = Depends(_tenant_admin),
    db=Depends(get_db),
) -> TenantSettingsOut:
    """UC-01 steps 1-2: show the organisation's current settings."""
    tenant = await settings_service.get_settings(db, tenant_id=admin.tenant_id)
    return TenantSettingsOut.model_validate(tenant)


@router.patch("/settings", response_model=TenantSettingsOut)
async def update_settings(
    payload: TenantSettingsUpdate,
    admin: CurrentUser = Depends(_tenant_admin),
    db=Depends(get_db),
) -> TenantSettingsOut:
    """UC-01 steps 3-4: validate and persist. The response IS the confirmation (step 5).

    There is no separate confirmation message: returning the saved record lets the client
    show what was stored rather than what was typed, which is exactly the distinction
    D-18 failed on — the old screen reported success without a request having been made.
    """
    tenant = await settings_service.update_settings(
        db,
        tenant_id=admin.tenant_id,
        name=payload.name,
        timezone=payload.timezone,
    )
    return TenantSettingsOut.model_validate(tenant)
