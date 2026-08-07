"""FastAPI application factory for the FlowDesk backend."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import ErrorEnvelopeMiddleware, register_exception_handlers
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

    # Order is load-bearing. `add_middleware` inserts at index 0, so the LAST call is the
    # OUTERMOST layer. Read this block bottom-up to follow a request:
    #   ProxyHeaders (fix the scheme) -> CORS (decorate every response, errors included)
    #   -> ErrorEnvelope (last resort) -> Starlette's typed handlers -> router.
    #
    # ErrorEnvelope must stay INSIDE CORS: an enveloped 500 with no allow-origin header is
    # still an opaque CORS failure in a browser, which is the half of D-13 that hurt.
    app.add_middleware(ErrorEnvelopeMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # D-15. Fly Proxy terminates TLS and forwards over the private 6PN network, so the ASGI
    # scope says scheme=http and Starlette's trailing-slash redirect builds an absolute
    # `http://` Location — which a browser on an HTTPS page refuses to follow as mixed
    # content while curl follows it happily. uvicorn mounts this same middleware itself,
    # but with trusted_hosts from FORWARDED_ALLOW_IPS (default "127.0.0.1"), and the 6PN
    # peer is not 127.0.0.1, so it never fires. Mounting it here keeps the fix in the repo
    # and under test (tests/test_proxy_headers.py) instead of in deploy configuration.
    #
    # trusted_hosts="*" trusts X-Forwarded-* from whoever opened the socket. On Fly that is
    # only Fly Proxy — the app never binds a public listener. Running FlowDesk without a
    # proxy in front would make both headers spoofable.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness probe used by Fly.io health checks."""
        return {"status": "ok"}

    return app


app = create_app()
