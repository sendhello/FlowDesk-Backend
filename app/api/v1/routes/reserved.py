"""Reserved Sprint-3 endpoints.

Declared now so the API contract with the frontend is stable, but they return 501 Not
Implemented until notifications (US-13, US-14) and analytics (US-15, US-16) are built.

These stubs live in their own module rather than alongside the real incident routes on
purpose: FastAPI resolves by registration order, so a leftover stub for a path that is now
implemented would either silently shadow the real handler or become dead code that still
pollutes /openapi.json with a duplicate operation_id. When US-14 ships this file shrinks
to analytics; when US-15/16 ship it is deleted.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.exceptions import NotImplementedYetError

router = APIRouter(tags=["reserved (Sprint 3)"])

_RESERVED = {
    "notifications": "In-app notifications (US-14).",
    "analytics/volume": "Incident volume analytics (US-15).",
    "analytics/status-distribution": "Status distribution analytics (US-16).",
}


def _make_reserved(description: str):
    async def _reserved() -> None:
        raise NotImplementedYetError(f"Reserved for a future sprint: {description}")

    return _reserved


for _path, _desc in _RESERVED.items():
    _slug = _path.replace("/", "_").replace("{", "").replace("}", "")
    for _method in ("GET", "POST"):
        router.add_api_route(
            f"/{_path}",
            _make_reserved(_desc),
            methods=[_method],
            status_code=501,
            name=f"reserved_{_method.lower()}_{_slug}",
            operation_id=f"reserved_{_method.lower()}_{_slug}",
            summary=f"[Reserved] {_desc}",
        )
