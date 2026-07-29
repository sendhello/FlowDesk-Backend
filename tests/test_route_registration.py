"""Route registration invariants, and proof that the reserved stubs are gone.

This file replaces tests/test_reserved.py. Its 501 assertions died with Sprint 3, but the
reason the file existed did not: FastAPI resolves by registration order, so a stub
registered before a real handler silently shadows it, and one registered after becomes
dead code that still pollutes /openapi.json with a duplicate operation_id. Those checks are
now written in a form that survives any future route rather than naming Sprint 3's paths.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.main import app
from app.models.enums import Role
from tests.conftest import login_as, seed_tenant, seed_user

LIVE_PATHS = [
    "/api/v1/incidents",
    "/api/v1/notifications",
    "/api/v1/notifications/unread-count",
    "/api/v1/analytics/volume",
    "/api/v1/analytics/status-distribution",
]

ANALYTICS_PATHS = [
    "/api/v1/analytics/volume",
    "/api/v1/analytics/status-distribution",
]


@pytest.mark.parametrize("path", LIVE_PATHS)
async def test_no_path_returns_501(client, db, path):
    """Every formerly reserved path is now implemented for a permitted caller."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.get(path)

    assert response.status_code == 200, response.text


def test_openapi_has_no_reserved_operations():
    """`routes/reserved.py` is deleted; nothing should still advertise itself as a stub."""
    schema = app.openapi()

    for path, methods in schema["paths"].items():
        for operation in methods.values():
            assert not operation.get("operationId", "").startswith("reserved_"), path
            assert not operation.get("summary", "").startswith("[Reserved]"), path


def test_openapi_operation_ids_are_unique():
    """The durable form of the shadowing guard: two handlers claiming one operation_id
    means one of them is unreachable, whatever caused it."""
    schema = app.openapi()
    ids = [
        operation["operationId"]
        for methods in schema["paths"].values()
        for operation in methods.values()
        if "operationId" in operation
    ]

    duplicates = [op_id for op_id, n in Counter(ids).items() if n > 1]
    assert duplicates == []


async def test_post_notifications_collection_returns_405(client, db):
    """Contract change for the frontend: notifications are system-generated, so there is
    no client-facing create. A client that could mint them could forge a notification
    against any incident."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.post("/api/v1/notifications", json={})

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("path", ANALYTICS_PATHS)
async def test_post_analytics_paths_return_405(client, db, path):
    """Analytics are reads. The reserved stubs used to answer POST with 501."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.post(path, json={})

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_notification_sub_paths_still_accept_post(client, db):
    """POST is gone from the /notifications COLLECTION only — the read actions remain."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)

    login_as(staff)
    response = await client.post("/api/v1/notifications/read-all")

    assert response.status_code == 200
