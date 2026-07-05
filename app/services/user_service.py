"""User management service (UC-03, US-03).

All reads/writes are tenant-scoped. Service functions take duck-typed `scope`/`actor`
objects (the deps.TenantScope / deps.CurrentUser dataclasses) — imported only under
TYPE_CHECKING to avoid a circular import with app.api.deps.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import log_privileged_action
from app.models.enums import Role, UserStatus
from app.models.user import User
from app.services.supabase_admin import (
    SupabaseAdminClient,
    SupabaseUserExistsError,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.api.deps import CurrentUser, TenantScope


async def get_by_id(db: AsyncSession, user_id: object) -> User | None:
    """Load a user by primary key (== Supabase auth id). Invalid id -> None."""
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except (ValueError, TypeError, AttributeError):
        return None
    return await db.get(User, uid)


async def get_by_email_in_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, email: str
) -> User | None:
    stmt = select(User).where(
        User.tenant_id == tenant_id, func.lower(User.email) == email.lower()
    )
    return await db.scalar(stmt)


def _out_of_scope(scope: "TenantScope", user: User) -> bool:
    if scope.is_system_admin:
        return False
    return user.tenant_id != scope.tenant_id


async def list_users(
    db: AsyncSession,
    *,
    scope: "TenantScope",
    target_tenant_id: uuid.UUID | None,
    role: Role | None,
    status: UserStatus | None,
    limit: int,
    offset: int,
) -> tuple[list[User], int]:
    conds = []
    if scope.is_system_admin:
        if target_tenant_id is not None:
            conds.append(User.tenant_id == target_tenant_id)
    else:
        conds.append(User.tenant_id == scope.tenant_id)
    if role is not None:
        conds.append(User.role == role)
    if status is not None:
        conds.append(User.status == status)

    total = await db.scalar(select(func.count()).select_from(User).where(*conds))
    rows = (
        await db.scalars(
            select(User).where(*conds).order_by(User.created_at).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def get_user(
    db: AsyncSession, *, scope: "TenantScope", user_id: uuid.UUID
) -> User:
    user = await db.get(User, user_id)
    if user is None or _out_of_scope(scope, user):
        raise NotFoundError("User not found.")
    return user


async def invite_user(
    db: AsyncSession,
    admin: SupabaseAdminClient,
    *,
    actor: "CurrentUser",
    scope: "TenantScope",
    email: str,
    name: str,
    role: Role,
    target_tenant_id: uuid.UUID | None = None,
) -> User:
    """Invite a new user into a tenant (UC-03 main flow). Saga with compensation."""
    # UC-03 E2: only a System Admin may create a System Admin.
    if role is Role.system_admin and actor.role is not Role.system_admin:
        raise ForbiddenError(
            "Only System Admins can create System Admin accounts.",
            details={"reason": "privilege_escalation"},
        )

    tenant_id = (
        (target_tenant_id or actor.tenant_id)
        if scope.is_system_admin
        else actor.tenant_id
    )

    # UC-03 E1: email unique within the tenant.
    if await get_by_email_in_tenant(db, tenant_id, email) is not None:
        raise ConflictError(
            "A user with this email already exists in your organisation."
        )

    created_auth = True
    try:
        auth_id = await admin.invite_user(email=email, name=name)
    except SupabaseUserExistsError:
        existing_id = await admin.get_user_by_email(email)
        if existing_id is None or await db.get(User, existing_id) is not None:
            raise ConflictError("A user with this email already exists.")
        auth_id, created_auth = existing_id, False

    try:
        user = User(
            id=auth_id,
            tenant_id=tenant_id,
            email=email,
            name=name,
            role=role,
            status=UserStatus.active,
        )
        db.add(user)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if created_auth:
            await admin.delete_user(auth_id)
        raise ConflictError(
            "A user with this email already exists in your organisation."
        )
    except Exception:
        await db.rollback()
        if created_auth:
            await admin.delete_user(auth_id)
        raise

    await db.refresh(user)
    log_privileged_action(
        "user_invite",
        actor_id=str(actor.id),
        user_id=str(user.id),
        tenant_id=str(tenant_id),
        role=role.value,
    )
    return user


async def update_user(
    db: AsyncSession,
    *,
    scope: "TenantScope",
    actor: "CurrentUser",
    user_id: uuid.UUID,
    name: str | None,
    role: Role | None,
) -> User:
    user = await get_user(db, scope=scope, user_id=user_id)
    if role is not None:
        if role is Role.system_admin and actor.role is not Role.system_admin:
            raise ForbiddenError(
                "Only System Admins can grant the System Admin role.",
                details={"reason": "privilege_escalation"},
            )
        user.role = role
    if name is not None:
        user.name = name
    await db.commit()
    await db.refresh(user)
    log_privileged_action("user_update", actor_id=str(actor.id), user_id=str(user.id))
    return user


async def set_status(
    db: AsyncSession,
    *,
    scope: "TenantScope",
    actor: "CurrentUser",
    user_id: uuid.UUID,
    status: UserStatus,
) -> User:
    user = await get_user(db, scope=scope, user_id=user_id)
    user.status = status
    await db.commit()
    await db.refresh(user)
    action = "user_deactivate" if status is UserStatus.inactive else "user_activate"
    log_privileged_action(action, actor_id=str(actor.id), user_id=str(user.id))
    return user
