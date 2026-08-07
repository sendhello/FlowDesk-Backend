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
    SupabaseAdminError,
    SupabaseUnavailableError,
    SupabaseUserExistsError,
    delete_user_best_effort,
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


async def _adopt_orphan_auth_user(
    db: AsyncSession,
    admin: SupabaseAdminClient,
    *,
    email: str,
    original: SupabaseUnavailableError,
) -> uuid.UUID:
    """Recover the auth id after a timed-out invite, or re-raise the original error (D-4).

    A timeout is the only upstream failure with an ambiguous outcome: a 429 or a 4xx means
    nothing happened, but a dropped connection may have left a created account and a sent
    invite email behind. Without this branch that email is trapped for good — every retry
    hits `already registered`, which surfaces as a 409 the caller cannot act on.

    Adopt ONLY an orphan: an auth account no `users` row points at. An auth id that already
    backs a FlowDesk user IS that user, and re-pointing it at a new row would hand one
    person's identity to another. That check is the whole safety argument.

    Deliberately NOT used by `tenant_service.register_organization`. That endpoint is
    public, and `SupabaseUnavailableError` also covers GoTrue's invite rate limit — which
    is cheap to trigger — so adoption there would let a stranger burn the limit and then
    bind someone else's address to an organisation of their choosing. See §7.3.
    """
    try:
        existing_id = await admin.get_user_by_email(email)
    except SupabaseAdminError:
        raise original from None
    if existing_id is None or await db.get(User, existing_id) is not None:
        raise original from None
    log_privileged_action(
        "auth_user_adopted",
        actor_id="system",
        auth_user_id=str(existing_id),
        email=email,
    )
    return existing_id


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
    except SupabaseUnavailableError as exc:
        # The invite may have landed before the connection dropped (D-4). `created_auth`
        # stays False: as far as we can prove the account predates this request, so a
        # later DB failure must not delete it — the next retry adopts it again.
        auth_id, created_auth = (
            await _adopt_orphan_auth_user(db, admin, email=email, original=exc),
            False,
        )

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
            await delete_user_best_effort(admin, auth_id, action="user_invite")
        raise ConflictError(
            "A user with this email already exists in your organisation."
        )
    except Exception:
        await db.rollback()
        if created_auth:
            await delete_user_best_effort(admin, auth_id, action="user_invite")
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


async def _count_active_tenant_admins(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    total = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.tenant_id == tenant_id,
            User.role == Role.tenant_admin,
            User.status == UserStatus.active,
        )
    )
    return int(total or 0)


async def _guard_last_tenant_admin(db: AsyncSession, *, user: User) -> None:
    """Refuse a change that would leave a tenant with no active Tenant Admin (D-3).

    Two routes reach the same brick: `/deactivate`, and `PATCH /users/{id}` demoting the
    last admin out of `tenant_admin` (promotion to `system_admin` counts — a System Admin
    is not scoped to the tenant, so it empties the admin pool just the same). One helper,
    called from both.

    Scoped by `user.tenant_id`, NEVER `scope.tenant_id`: a System Admin's scope carries
    `tenant_id=None`, so counting on the scope would find zero admins and wave through
    exactly the call that most needs the guard — a System Admin deactivating some other
    tenant's last administrator. `workflow_service.reassign` sets the same precedent.

    Check-then-act, so two concurrent requests against the last two admins can both pass.
    Closing that needs a row lock or a deferred constraint, which is disproportionate here:
    unlike the `delete_category` race there is no database constraint to fall back on, and
    the recovery (another admin, or the Supabase dashboard) is the same either way.
    """
    if user.role is not Role.tenant_admin or user.status is not UserStatus.active:
        return
    if await _count_active_tenant_admins(db, user.tenant_id) > 1:
        return
    raise ConflictError(
        "This is the organisation's last active administrator. Appoint another "
        "administrator before changing this account.",
        details={"reason": "last_tenant_admin"},
    )


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
        # Before the assignment, so the count sees committed state regardless of autoflush.
        if role is not Role.tenant_admin:
            await _guard_last_tenant_admin(db, user=user)
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
    # Gated on the TRANSITION, not the target state. Re-deactivating an already-inactive
    # user stays idempotent (§4.4: 200 and the same body), and activation is never blocked
    # — a guard on `status is inactive` alone would break both, including the call that
    # recovers a tenant whose admins are all deactivated.
    if status is UserStatus.inactive and user.status is UserStatus.active:
        if user.id == actor.id:
            raise ForbiddenError(
                "You cannot deactivate your own account.",
                details={"reason": "self_deactivation"},
            )
        await _guard_last_tenant_admin(db, user=user)
    user.status = status
    await db.commit()
    await db.refresh(user)
    action = "user_deactivate" if status is UserStatus.inactive else "user_activate"
    log_privileged_action(action, actor_id=str(actor.id), user_id=str(user.id))
    return user
