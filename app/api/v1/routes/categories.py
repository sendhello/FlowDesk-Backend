"""Category routes (UC-04, US-02). Scoped to the caller's tenant."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import (
    CurrentUser,
    PageParams,
    get_current_user,
    pagination_params,
    require_role,
)
from app.db.session import get_db
from app.models.enums import Role
from app.schemas.category import CategoryCreate, CategoryOut, CategoryUpdate
from app.schemas.common import Page, Pagination
from app.services import category_service

router = APIRouter(tags=["categories"])

_tenant_admin = require_role(Role.tenant_admin)


@router.get("/categories", response_model=Page[CategoryOut])
async def list_categories(
    page: PageParams = Depends(pagination_params),
    user: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
) -> Page[CategoryOut]:
    rows, total = await category_service.list_categories(
        db, tenant_id=user.tenant_id, limit=page.limit, offset=page.offset
    )
    return Page[CategoryOut](
        data=[CategoryOut.model_validate(r) for r in rows],
        pagination=Pagination(limit=page.limit, offset=page.offset, total=total),
    )


@router.post(
    "/categories", response_model=CategoryOut, status_code=status.HTTP_201_CREATED
)
async def create_category(
    payload: CategoryCreate,
    admin: CurrentUser = Depends(_tenant_admin),
    db=Depends(get_db),
) -> CategoryOut:
    category = await category_service.create_category(
        db, tenant_id=admin.tenant_id, name=payload.name, description=payload.description
    )
    return CategoryOut.model_validate(category)


@router.get("/categories/{category_id}", response_model=CategoryOut)
async def get_category(
    category_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db=Depends(get_db),
) -> CategoryOut:
    category = await category_service.get_category(
        db, tenant_id=user.tenant_id, category_id=category_id
    )
    return CategoryOut.model_validate(category)


@router.patch("/categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    admin: CurrentUser = Depends(_tenant_admin),
    db=Depends(get_db),
) -> CategoryOut:
    category = await category_service.update_category(
        db,
        tenant_id=admin.tenant_id,
        category_id=category_id,
        name=payload.name,
        description=payload.description,
    )
    return CategoryOut.model_validate(category)


@router.delete(
    "/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_category(
    category_id: uuid.UUID,
    admin: CurrentUser = Depends(_tenant_admin),
    db=Depends(get_db),
) -> None:
    await category_service.delete_category(
        db, tenant_id=admin.tenant_id, category_id=category_id
    )
