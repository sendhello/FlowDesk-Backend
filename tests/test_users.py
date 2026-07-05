"""Tenant-admin user management (UC-03, US-03)."""

from __future__ import annotations

from app.models.enums import Role
from tests.conftest import login_as, seed_tenant, seed_user


async def test_tenant_admin_invites_staff(client, db, fake_supabase):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    resp = await client.post(
        "/api/v1/users",
        json={"email": "new@acme.com", "name": "New Hire", "role": "staff"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "staff"
    assert resp.json()["status"] == "active"
    assert len(fake_supabase.invited) == 1


async def test_duplicate_email_in_tenant_returns_409(client, db, fake_supabase):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin, email="dup@acme.com")
    login_as(admin)

    resp = await client.post(
        "/api/v1/users",
        json={"email": "dup@acme.com", "name": "Dup", "role": "staff"},
    )
    assert resp.status_code == 409


async def test_tenant_admin_cannot_create_system_admin_403(client, db, fake_supabase):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    resp = await client.post(
        "/api/v1/users",
        json={"email": "x@acme.com", "name": "X", "role": "system_admin"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["reason"] == "privilege_escalation"


async def test_update_user_role(client, db, fake_supabase):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    target = await seed_user(db, tenant, Role.staff)
    login_as(admin)

    resp = await client.patch(
        f"/api/v1/users/{target.id}", json={"role": "reviewer"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "reviewer"


async def test_deactivate_user(client, db, fake_supabase):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    target = await seed_user(db, tenant, Role.staff)
    login_as(admin)

    resp = await client.post(f"/api/v1/users/{target.id}/deactivate")
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"
