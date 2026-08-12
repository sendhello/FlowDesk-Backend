"""Tenant workspace settings (D-18). Always scoped to the caller's own tenant.

The organisation name lives on `tenants.name`, which is globally unique, so a rename can
collide with another organisation exactly the way a registration can. It is answered with
the same message and the same `organization_name_taken` slug that
`tenant_service.register_organization` uses — one conflict, one vocabulary, whichever door
the client came through.
"""

from __future__ import annotations

import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, UnauthorizedError, ValidationError
from app.models.tenant import Tenant

_NAME_TAKEN_MESSAGE = "An organisation with this name already exists."


def _name_taken() -> ConflictError:
    return ConflictError(
        _NAME_TAKEN_MESSAGE, details={"reason": "organization_name_taken"}
    )


def _validated_timezone(value: str) -> str:
    """Reject a zone PostgreSQL would later choke on.

    Validated here rather than with a pydantic validator so the refusal can carry
    `details.reason`. A `field_validator` raises `RequestValidationError`, whose envelope
    has a `details.errors` slot and no `reason` — the same reasoning that keeps UC-08 E2 in
    `workflow_service` (see `TransitionCreate`).

    Python's zoneinfo and PostgreSQL both read the IANA tzdb, so a name accepted here is a
    name `AT TIME ZONE` accepts. Without this check a typo would be stored happily and then
    fail on every analytics call afterwards — a settings screen that can break a different
    page is worse than one that refuses.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            "That is not a recognised IANA timezone name.",
            details={"reason": "invalid_timezone"},
        )
    return value


async def get_settings(db: AsyncSession, *, tenant_id: uuid.UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        # Identical condition and identical answer to `GET /me`: the caller authenticated
        # against a tenant that no longer exists, so the session is broken rather than the
        # request being wrong. Unreachable through the API — `users.tenant_id` is a
        # foreign key — but a 401 here beats an AttributeError.
        raise UnauthorizedError(
            "User tenant not found.", details={"reason": "tenant_not_found"}
        )
    return tenant


async def update_settings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str | None,
    timezone: str | None,
) -> Tenant:
    """Apply a partial update to the caller's own organisation (UC-01 steps 3-4)."""
    tenant = await get_settings(db, tenant_id=tenant_id)

    if timezone is not None:
        tenant.timezone = _validated_timezone(timezone)

    if name is not None and name != tenant.name:
        # Case-insensitive pre-check, mirroring registration: the unique constraint is
        # exact, so "Acme" and "acme" would both be accepted by the database and are the
        # same organisation to a human. Excluding self keeps a no-op rename from
        # colliding with itself.
        clash = await db.scalar(
            select(Tenant.id).where(
                func.lower(Tenant.name) == name.lower(), Tenant.id != tenant.id
            )
        )
        if clash is not None:
            raise _name_taken()
        tenant.name = name

    try:
        await db.commit()
    except IntegrityError:
        # Another organisation registered the same name between the SELECT and the COMMIT.
        # Same answer either way, so the caller cannot tell the race from the ordinary
        # case — as in create_category and register_organization.
        await db.rollback()
        raise _name_taken()
    await db.refresh(tenant)
    return tenant
