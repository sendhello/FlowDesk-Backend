"""Aggregate the v1 API under the /api/v1 prefix."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    analytics,
    auth,
    categories,
    incidents,
    notifications,
    organizations,
    users,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(organizations.router)
api_router.include_router(categories.router)
api_router.include_router(users.router)
api_router.include_router(incidents.router)
api_router.include_router(notifications.router)
api_router.include_router(analytics.router)
