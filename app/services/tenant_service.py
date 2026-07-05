"""Tenant provisioning (UC-02, US-01).

Registration is a create-then-compensate saga because the Supabase Admin call (HTTP) and
the tenant/user inserts (SQL) cannot share one ACID transaction: create the auth user
first, then insert tenant + admin user in one DB transaction, and if the DB step fails,
delete the auth user so no orphan is left behind (Part 3A.3).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.core.logging import log_privileged_action
from app.models.enums import Role, UserStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.services.supabase_admin import (
    SupabaseAdminClient,
    SupabaseUserExistsError,
)


async def register_organization(
    db: AsyncSession,
    admin: SupabaseAdminClient,
    *,
    organization_name: str,
    admin_email: str,
    admin_name: str,
) -> tuple[Tenant, User]:
    # 1. Pre-validate a unique organisation name (UC-02 E1). Cheap, fail fast.
    existing = await db.scalar(
        select(Tenant.id).where(func.lower(Tenant.name) == organization_name.lower())
    )
    if existing is not None:
        raise ConflictError("An organisation with this name already exists.")

    # 2. Create the Supabase auth user (invite flow -> GoTrue emails a set-password link).
    try:
        auth_id = await admin.invite_user(email=admin_email, name=admin_name)
    except SupabaseUserExistsError:
        raise ConflictError(
            "A user with this email is already registered.",
        )

    # 3. Insert tenant + admin user in one transaction; compensate on failure.
    try:
        tenant = Tenant(name=organization_name)
        db.add(tenant)
        await db.flush()  # assign tenant.id
        user = User(
            id=auth_id,
            tenant_id=tenant.id,
            email=admin_email,
            name=admin_name,
            role=Role.tenant_admin,
            status=UserStatus.active,
        )
        db.add(user)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await admin.delete_user(auth_id)  # compensation — no orphan
        raise ConflictError("An organisation with this name already exists.")
    except Exception:
        await db.rollback()
        await admin.delete_user(auth_id)  # compensation — no orphan
        raise

    await db.refresh(tenant)
    await db.refresh(user)
    log_privileged_action(
        "org_register", actor_id=str(auth_id), tenant_id=str(tenant.id)
    )
    return tenant, user
