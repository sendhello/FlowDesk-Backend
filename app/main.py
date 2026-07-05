"""FastAPI application factory for the FlowDesk backend."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging

DESCRIPTION = (
    "FlowDesk — multi-tenant SaaS incident & workflow management platform. "
    "This backend is the single point of enforcement for authentication, RBAC "
    "and tenant isolation. Auth is delegated to Supabase; the frontend sends the "
    "Supabase JWT in the Authorization header and this API verifies it on every request."
)


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="FlowDesk API", version="0.1.0", description=DESCRIPTION)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness probe used by Fly.io health checks."""
        return {"status": "ok"}

    return app


app = create_app()
