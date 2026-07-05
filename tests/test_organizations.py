"""Organisation registration saga (UC-02, US-01) + NFR-05."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.enums import Role
from app.models.tenant import Tenant
from tests.conftest import seed_tenant, seed_user


async def test_register_organization_201(client, db, fake_supabase):
    resp = await client.post(
        "/api/v1/organizations",
        json={
            "organization_name": "NewCo",
            "admin_email": "admin@newco.com",
            "admin_name": "Admin",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant"]["name"] == "NewCo"
    assert body["admin_user"]["role"] == "tenant_admin"
    # users.id == the Supabase auth id (Decision 1).
    assert body["admin_user"]["id"] == str(fake_supabase.invited[0])
    # NFR-05: no password or token leaked in the response.
    assert "password" not in resp.text.lower()
    assert "token" not in resp.text.lower()


async def test_duplicate_org_name_returns_409(client, db, fake_supabase):
    await seed_tenant(db, "Acme")
    resp = await client.post(
        "/api/v1/organizations",
        json={
            "organization_name": "Acme",
            "admin_email": "a@a.com",
            "admin_name": "A",
        },
    )
    assert resp.status_code == 409
    # The name collision is caught before creating an auth user.
    assert fake_supabase.invited == []


async def test_invalid_email_returns_422(client, db, fake_supabase):
    resp = await client.post(
        "/api/v1/organizations",
        json={
            "organization_name": "SomeCo",
            "admin_email": "not-an-email",
            "admin_name": "A",
        },
    )
    assert resp.status_code == 422


async def test_saga_compensates_on_db_failure(client, db, fake_supabase):
    # Force the users INSERT to fail with a PK conflict by making the fake return an id
    # that already exists as a user, then assert the auth user is deleted (no orphan).
    tenant = await seed_tenant(db, "Existing")
    existing = await seed_user(db, tenant, Role.staff)
    fake_supabase.next_id = existing.id

    resp = await client.post(
        "/api/v1/organizations",
        json={
            "organization_name": "BrandNew",
            "admin_email": "new@brandnew.com",
            "admin_name": "A",
        },
    )
    assert resp.status_code == 409
    # Compensation: the auth user we created was deleted.
    assert existing.id in fake_supabase.deleted
    # No orphan tenant persisted.
    count = await db.scalar(
        select(func.count()).select_from(Tenant).where(Tenant.name == "BrandNew")
    )
    assert count == 0
