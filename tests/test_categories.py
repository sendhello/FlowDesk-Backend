"""Category CRUD + RBAC (UC-04, US-02, NFR-10)."""

from __future__ import annotations

from app.models.enums import Role
from tests.conftest import login_as, seed_tenant, seed_user


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
