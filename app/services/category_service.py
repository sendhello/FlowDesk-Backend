"""Incident category service (UC-04, US-02). Always scoped to a single tenant."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models.category import Category
from app.models.incident import Incident


async def list_categories(
    db: AsyncSession, *, tenant_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[Category], int]:
    cond = Category.tenant_id == tenant_id
    total = await db.scalar(select(func.count()).select_from(Category).where(cond))
    rows = (
        await db.scalars(
            select(Category).where(cond).order_by(Category.name).limit(limit).offset(offset)
        )
    ).all()
    return list(rows), int(total or 0)


async def get_category(
    db: AsyncSession, *, tenant_id: uuid.UUID, category_id: uuid.UUID
) -> Category:
    category = await db.get(Category, category_id)
    if category is None or category.tenant_id != tenant_id:
        raise NotFoundError("Category not found.")
    return category


async def create_category(
    db: AsyncSession, *, tenant_id: uuid.UUID, name: str, description: str | None
) -> Category:
    category = Category(tenant_id=tenant_id, name=name, description=description)
    db.add(category)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("A category with this name already exists.")
    await db.refresh(category)
    return category


async def update_category(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    category_id: uuid.UUID,
    name: str | None,
    description: str | None,
) -> Category:
    category = await get_category(db, tenant_id=tenant_id, category_id=category_id)
    if name is not None:
        category.name = name
    if description is not None:
        category.description = description
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("A category with this name already exists.")
    await db.refresh(category)
    return category


_IN_USE_MESSAGE = "This category is referenced by existing incidents and cannot be deleted."


def _in_use() -> ConflictError:
    return ConflictError(_IN_USE_MESSAGE, details={"reason": "category_in_use"})


async def delete_category(
    db: AsyncSession, *, tenant_id: uuid.UUID, category_id: uuid.UUID
) -> None:
    category = await get_category(db, tenant_id=tenant_id, category_id=category_id)
    # UC-04 A2: block deletion while ANY incident references the category.
    #
    # The guard used to exclude closed incidents, which reads as a fair interpretation of
    # "open incidents" — but `incidents.category_id` is a plain foreign key with no ON
    # DELETE action, so a closed incident blocks the DELETE exactly as hard as an open one.
    # The category passed the guard and then died on the constraint, uncaught: a plain-text
    # 500 (D-2). This is a hard delete of a label, not of history, and a closed incident
    # still needs its category to render.
    blocking = await db.scalar(
        select(Incident.id).where(Incident.category_id == category_id).limit(1)
    )
    if blocking is not None:
        raise _in_use()
    await db.delete(category)
    try:
        await db.commit()
    except IntegrityError:
        # An incident filed between the SELECT and the COMMIT. Same answer either way, so
        # the caller cannot tell the race from the ordinary case — which is the point.
        # Mirrors create_category and update_category above.
        await db.rollback()
        raise _in_use()
