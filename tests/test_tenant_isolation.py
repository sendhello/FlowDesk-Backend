"""Cross-tenant isolation (NFR-12). A tenant admin must never reach another tenant's
data: cross-tenant access returns 404 (existence is not leaked)."""

from __future__ import annotations

from app.models.enums import Role
from tests.conftest import login_as, seed_tenant, seed_user


async def test_cross_tenant_category_access_returns_404(client, db):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    admin_a = await seed_user(db, tenant_a, Role.tenant_admin, email="a@acme.com")
    admin_b = await seed_user(db, tenant_b, Role.tenant_admin, email="b@globex.com")

    # Create a category inside tenant B.
    login_as(admin_b)
    created = await client.post("/api/v1/categories", json={"name": "B-only"})
    cat_b_id = created.json()["id"]

    # Tenant A's admin must not see or touch it.
    login_as(admin_a)
    assert (await client.get(f"/api/v1/categories/{cat_b_id}")).status_code == 404
    assert (
        await client.patch(f"/api/v1/categories/{cat_b_id}", json={"name": "hack"})
    ).status_code == 404
    assert (await client.delete(f"/api/v1/categories/{cat_b_id}")).status_code == 404

    # A's own list is empty (no leakage of B's rows).
    listed = await client.get("/api/v1/categories")
    assert listed.json()["pagination"]["total"] == 0


async def test_cross_tenant_user_access_returns_404(client, db, fake_supabase):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    admin_a = await seed_user(db, tenant_a, Role.tenant_admin, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="s@globex.com")

    login_as(admin_a)
    assert (await client.get(f"/api/v1/users/{staff_b.id}")).status_code == 404
    listed = await client.get("/api/v1/users")
    ids = {row["id"] for row in listed.json()["data"]}
    assert str(staff_b.id) not in ids
