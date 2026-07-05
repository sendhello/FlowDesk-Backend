"""GET /me — DB-backed identity resolution (US-05, UC-05 E2)."""

from __future__ import annotations

import uuid

from app.models.enums import Role, UserStatus
from tests.conftest import login_claims, seed_tenant, seed_user


async def test_me_returns_role_and_tenant(client, db):
    tenant = await seed_tenant(db, "Acme")
    user = await seed_user(db, tenant, Role.tenant_admin, email="admin@acme.com")
    login_claims(str(user.id))

    resp = await client.get("/api/v1/me")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(user.id)
    assert body["role"] == "tenant_admin"
    assert body["tenant"]["name"] == "Acme"


async def test_me_unprovisioned_sub_returns_401(client, db):
    login_claims(str(uuid.uuid4()))
    resp = await client.get("/api/v1/me")
    assert resp.status_code == 401


async def test_me_inactive_user_returns_403(client, db):
    tenant = await seed_tenant(db, "Acme")
    user = await seed_user(db, tenant, Role.staff, status=UserStatus.inactive)
    login_claims(str(user.id))

    resp = await client.get("/api/v1/me")

    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["reason"] == "account_deactivated"
