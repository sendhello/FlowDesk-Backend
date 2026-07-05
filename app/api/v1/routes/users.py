"""User management routes (UC-03, US-03)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    CurrentUser,
    PageParams,
    TenantScope,
    pagination_params,
    require_role,
    tenant_scope,
)
from app.db.session import get_db
from app.models.enums import Role, UserStatus
from app.schemas.common import Page, Pagination
from app.schemas.user import UserInvite, UserOut, UserUpdate
from app.services import user_service
from app.services.supabase_admin import SupabaseAdminClient, get_supabase_admin

router = APIRouter(tags=["users"])

# Both Tenant Admin (own tenant) and System Admin (cross-tenant) may manage users.
_admin = require_role(Role.tenant_admin, Role.system_admin)


@router.get("/users", response_model=Page[UserOut])
async def list_users(
    page: PageParams = Depends(pagination_params),
    role: Role | None = Query(default=None),
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    tenant_id: uuid.UUID | None = Query(default=None),
    actor: CurrentUser = Depends(_admin),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> Page[UserOut]:
    rows, total = await user_service.list_users(
        db,
        scope=scope,
        target_tenant_id=tenant_id,
        role=role,
        status=status_filter,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[UserOut](
        data=[UserOut.model_validate(r) for r in rows],
        pagination=Pagination(limit=page.limit, offset=page.offset, total=total),
    )


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: UserInvite,
    tenant_id: uuid.UUID | None = Query(default=None),
    actor: CurrentUser = Depends(_admin),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
    admin: SupabaseAdminClient = Depends(get_supabase_admin),
) -> UserOut:
    user = await user_service.invite_user(
        db,
        admin,
        actor=actor,
        scope=scope,
        email=str(payload.email),
        name=payload.name,
        role=payload.role,
        target_tenant_id=tenant_id,
    )
    return UserOut.model_validate(user)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: uuid.UUID,
    actor: CurrentUser = Depends(_admin),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> UserOut:
    user = await user_service.get_user(db, scope=scope, user_id=user_id)
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    actor: CurrentUser = Depends(_admin),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> UserOut:
    user = await user_service.update_user(
        db,
        scope=scope,
        actor=actor,
        user_id=user_id,
        name=payload.name,
        role=payload.role,
    )
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: uuid.UUID,
    actor: CurrentUser = Depends(_admin),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> UserOut:
    user = await user_service.set_status(
        db, scope=scope, actor=actor, user_id=user_id, status=UserStatus.inactive
    )
    return UserOut.model_validate(user)


@router.post("/users/{user_id}/activate", response_model=UserOut)
async def activate_user(
    user_id: uuid.UUID,
    actor: CurrentUser = Depends(_admin),
    scope: TenantScope = Depends(tenant_scope),
    db=Depends(get_db),
) -> UserOut:
    user = await user_service.set_status(
        db, scope=scope, actor=actor, user_id=user_id, status=UserStatus.active
    )
    return UserOut.model_validate(user)
