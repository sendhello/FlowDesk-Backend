"""Auth / session routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, get_current_user
from app.core.exceptions import UnauthorizedError
from app.db.session import get_db
from app.models.tenant import Tenant
from app.schemas.auth import MeResponse, TenantRef

router = APIRouter(tags=["auth"])


@router.get("/me", response_model=MeResponse)
async def read_me(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """Resolve the authenticated caller's identity, role and tenant (US-05)."""
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None:
        raise UnauthorizedError("User tenant not found.")
    return MeResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        status=user.status,
        tenant=TenantRef(id=tenant.id, name=tenant.name),
    )
