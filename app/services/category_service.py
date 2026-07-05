"""Incident category service (UC-04, US-02). Always scoped to a single tenant."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models.category import Category
from app.models.enums import IncidentStatus
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


async def delete_category(
    db: AsyncSession, *, tenant_id: uuid.UUID, category_id: uuid.UUID
) -> None:
    category = await get_category(db, tenant_id=tenant_id, category_id=category_id)
    # UC-04 A2: block deletion while non-closed incidents reference the category.
    blocking = await db.scalar(
        select(Incident.id)
        .where(
            Incident.category_id == category_id,
            Incident.status != IncidentStatus.closed,
        )
        .limit(1)
    )
    if blocking is not None:
        raise ConflictError(
            "This category is used by open incidents and cannot be deleted."
        )
    await db.delete(category)
    await db.commit()
