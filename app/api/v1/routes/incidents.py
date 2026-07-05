"""Reserved Sprint-2/3 endpoints.

Declared now so the API contract with the frontend is stable, but they return
501 Not Implemented until the incident, workflow, notification and analytics features
are built (US-08..US-16).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.exceptions import NotImplementedYetError

router = APIRouter(tags=["reserved (Sprint 2/3)"])

_RESERVED = {
    "incidents": "Incident submission and listing (US-08, US-09, US-12).",
    "incidents/{incident_id}": "Incident detail (US-12).",
    "incidents/{incident_id}/transitions": "Workflow transitions (US-10, US-11).",
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
