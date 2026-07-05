"""Organisation registration route (UC-02, US-01). Public / unauthenticated."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.organization import (
    AdminUserOut,
    OrgRegisterRequest,
    OrgRegisterResponse,
    TenantOut,
)
from app.services import tenant_service
from app.services.supabase_admin import SupabaseAdminClient, get_supabase_admin

router = APIRouter(tags=["organizations"])


@router.post(
    "/organizations",
    response_model=OrgRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_organization(
    payload: OrgRegisterRequest,
    db: AsyncSession = Depends(get_db),
    admin: SupabaseAdminClient = Depends(get_supabase_admin),
) -> OrgRegisterResponse:
    """Register a new organisation and its first Tenant Admin. No password is returned;
    Supabase emails the admin a set-password link."""
    tenant, user = await tenant_service.register_organization(
        db,
        admin,
        organization_name=payload.organization_name,
        admin_email=str(payload.admin_email),
        admin_name=payload.admin_name,
    )
    return OrgRegisterResponse(
        tenant=TenantOut.model_validate(tenant),
        admin_user=AdminUserOut.model_validate(user),
    )
