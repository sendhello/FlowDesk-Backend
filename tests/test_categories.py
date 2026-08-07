"""Category CRUD + RBAC (UC-04, US-02, NFR-10)."""

from __future__ import annotations

import pytest

from app.models.enums import IncidentStatus, Role
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)


async def test_create_and_list_category(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    created = await client.post(
        "/api/v1/categories", json={"name": "Network", "description": "Net issues"}
    )
    assert created.status_code == 201, created.text

    listed = await client.get("/api/v1/categories")
    assert listed.status_code == 200
    assert listed.json()["pagination"]["total"] == 1
    assert listed.json()["data"][0]["name"] == "Network"


async def test_duplicate_category_name_returns_409(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    await client.post("/api/v1/categories", json={"name": "Network"})
    dup = await client.post("/api/v1/categories", json={"name": "Network"})
    assert dup.status_code == 409


async def test_staff_cannot_create_category_403(client, db):
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    login_as(staff)

    resp = await client.post("/api/v1/categories", json={"name": "Network"})
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["reason"] == "insufficient_role"


async def test_update_and_delete_category(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    created = await client.post("/api/v1/categories", json={"name": "Network"})
    cid = created.json()["id"]

    updated = await client.patch(
        f"/api/v1/categories/{cid}", json={"description": "updated"}
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "updated"

    deleted = await client.delete(f"/api/v1/categories/{cid}")
    assert deleted.status_code == 204


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(IncidentStatus.open, id="open"),
        pytest.param(IncidentStatus.in_review, id="in_review"),
        pytest.param(IncidentStatus.closed, id="closed"),
    ],
)
async def test_delete_referenced_category_returns_409(client, db, status):
    """D-2. The `closed` case answered a plain-text 500 until Sprint 4.

    `incidents.category_id` is a plain FK with no ON DELETE action, so an incident of ANY
    status blocks the delete at the database. The guard only excluded closed ones, so a
    category referenced solely by closed incidents sailed past it and died on the
    constraint with nothing to catch it.
    """
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff, email="staff@acme.com")
    category = await seed_category(db, tenant, name="Network")
    await seed_incident(
        db, tenant, category=category, submitted_by=staff, status=status
    )
    login_as(admin)

    resp = await client.delete(f"/api/v1/categories/{category.id}")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "category_in_use"


async def test_a_refused_delete_leaves_the_category_intact(client, db):
    """Proves the refusal is a refusal, not a half-applied delete."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff, email="staff@acme.com")
    category = await seed_category(db, tenant, name="Network")
    await seed_incident(
        db, tenant, category=category, submitted_by=staff, status=IncidentStatus.closed
    )
    login_as(admin)

    assert (await client.delete(f"/api/v1/categories/{category.id}")).status_code == 409

    still_there = await client.get(f"/api/v1/categories/{category.id}")
    assert still_there.status_code == 200, still_there.text
    assert still_there.json()["name"] == "Network"
