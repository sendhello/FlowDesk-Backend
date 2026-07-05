"""Reusable FastAPI dependencies: authentication, RBAC and tenant scoping.

This module is the single seam through which every protected request passes, so it is
where NFR-10 (RBAC at the backend), NFR-11 (JWT expiry -> 401) and NFR-12 (tenant
scoping) are enforced.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.logging import log_auth_failure
from app.core.security import verify_jwt
from app.db.session import get_db
from app.models.enums import Role, UserStatus
from app.services import user_service

# auto_error=False so a missing/blank Authorization header yields our 401 envelope,
# not FastAPI's default 403.
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller, resolved from the DB on every request."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    name: str
    role: Role
    status: UserStatus


@dataclass(frozen=True)
class TenantScope:
    """Whether the caller may cross tenants (System Admin) or is pinned to one."""

    is_system_admin: bool
    tenant_id: uuid.UUID | None  # None only for a System Admin ("all tenants")


@dataclass(frozen=True)
class PageParams:
    limit: int
    offset: int


async def get_current_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Verify the Supabase JWT (signature, issuer, audience, expiry). US-07 / NFR-11."""
    if credentials is None or not credentials.credentials:
        log_auth_failure("missing_token")
        raise UnauthorizedError(
            "Missing authentication token.", details={"reason": "missing_token"}
        )
    try:
        return verify_jwt(credentials.credentials)
    except UnauthorizedError as exc:
        log_auth_failure(str(exc.details.get("reason", "invalid_token")))
        raise


async def get_current_user(
    claims: dict = Depends(get_current_claims),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Resolve identity -> (tenant_id, role, status) from the DB every request.

    Role/status come from the database, not the token, so a role change or deactivation
    takes effect on the user's next request (UC-03 A1.2, UC-05 E2).
    """
    sub = claims.get("sub")
    user = await user_service.get_by_id(db, sub)
    if user is None:
        log_auth_failure("user_not_provisioned", sub=sub)
        raise UnauthorizedError(
            "User is not provisioned.", details={"reason": "user_not_provisioned"}
        )
    if user.status is not UserStatus.active:
        log_auth_failure("account_deactivated", user_id=str(user.id))
        raise ForbiddenError(
            "Your account has been deactivated. Contact your administrator.",
            details={"reason": "account_deactivated"},
        )
    return CurrentUser(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        name=user.name,
        role=user.role,
        status=user.status,
    )


def require_role(*allowed: Role):
    """Dependency factory: allow only the given roles. NFR-10."""

    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            raise ForbiddenError(
                "You do not have permission to perform this action.",
                details={
                    "reason": "insufficient_role",
                    "required": [r.value for r in allowed],
                },
            )
        return user

    return _dep


def tenant_scope(user: CurrentUser = Depends(get_current_user)) -> TenantScope:
    """Compute the caller's tenant scope. System Admin is cross-tenant. NFR-12."""
    if user.role is Role.system_admin:
        return TenantScope(is_system_admin=True, tenant_id=None)
    return TenantScope(is_system_admin=False, tenant_id=user.tenant_id)


def pagination_params(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PageParams:
    return PageParams(limit=limit, offset=offset)
