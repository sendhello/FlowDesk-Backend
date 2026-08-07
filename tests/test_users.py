"""Tenant-admin user management (UC-03, US-03)."""

from __future__ import annotations

from app.models.enums import Role, UserStatus
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


# ---- D-3: nobody may brick a tenant -----------------------------------------------


async def test_admin_cannot_deactivate_themselves_403(client, db, fake_supabase):
    """Deactivation takes effect on the very next request, including the one that would
    undo it — so self-deactivation was a one-way door."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    await seed_user(db, tenant, Role.tenant_admin, email="second@acme.com")
    login_as(admin)

    resp = await client.post(f"/api/v1/users/{admin.id}/deactivate")

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["reason"] == "self_deactivation"


async def test_a_tenants_last_admin_cannot_be_deactivated_409(client, db, fake_supabase):
    """The guard must count within the TARGET's tenant, not the actor's scope.

    Only a System Admin can reach this state — a tenant's sole admin cannot deactivate
    themselves (that is the 403 above), so someone cross-tenant has to try it. And a System
    Admin's `TenantScope` carries `tenant_id=None`: counting on the scope instead of on
    `user.tenant_id` finds zero admins, passes the guard, and bricks the tenant. This test
    is what fails if that is ever refactored.
    """
    acme = await seed_tenant(db, "Acme")
    other = await seed_tenant(db, "Globex")
    sole_admin = await seed_user(db, acme, Role.tenant_admin, email="only@acme.com")
    await seed_user(db, acme, Role.staff, email="staff@acme.com")
    root = await seed_user(db, other, Role.system_admin, email="root@globex.com")
    login_as(root)

    resp = await client.post(f"/api/v1/users/{sole_admin.id}/deactivate")

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "last_tenant_admin"


async def test_a_second_admin_can_still_be_deactivated_200(client, db, fake_supabase):
    """The guard blocks the last one, not admins in general."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    spare = await seed_user(db, tenant, Role.tenant_admin, email="spare@acme.com")
    login_as(admin)

    resp = await client.post(f"/api/v1/users/{spare.id}/deactivate")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "inactive"


async def test_deactivating_an_inactive_admin_stays_idempotent_200(
    client, db, fake_supabase
):
    """§4.4 promises idempotency. The guards fire on the transition, not the target state,
    so a no-op deactivation must not start returning 409."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    dormant = await seed_user(
        db,
        tenant,
        Role.tenant_admin,
        email="dormant@acme.com",
        status=UserStatus.inactive,
    )
    login_as(admin)

    resp = await client.post(f"/api/v1/users/{dormant.id}/deactivate")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "inactive"


async def test_activation_is_never_blocked_200(client, db, fake_supabase):
    """Activation is the recovery path. Blocking it would be the worst possible bug here."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    dormant = await seed_user(
        db,
        tenant,
        Role.tenant_admin,
        email="dormant@acme.com",
        status=UserStatus.inactive,
    )
    login_as(admin)

    resp = await client.post(f"/api/v1/users/{dormant.id}/activate")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"


async def test_the_last_admin_cannot_be_demoted_409(client, db, fake_supabase):
    """Same brick, different route: PATCH the sole admin down to staff."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    resp = await client.patch(f"/api/v1/users/{admin.id}", json={"role": "staff"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "last_tenant_admin"


async def test_the_last_admin_cannot_be_promoted_to_system_admin_409(
    client, db, fake_supabase
):
    """A System Admin is not scoped to the tenant, so promoting the sole Tenant Admin
    empties the tenant's admin pool just as surely as demoting them."""
    acme = await seed_tenant(db, "Acme")
    other = await seed_tenant(db, "Globex")
    sole_admin = await seed_user(db, acme, Role.tenant_admin, email="only@acme.com")
    root = await seed_user(db, other, Role.system_admin, email="root@globex.com")
    login_as(root)

    resp = await client.patch(
        f"/api/v1/users/{sole_admin.id}", json={"role": "system_admin"}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["details"]["reason"] == "last_tenant_admin"


async def test_demoting_a_non_admin_is_unaffected_200(client, db, fake_supabase):
    """Regression guard: the helper returns early for anyone who is not an active admin."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff, email="staff@acme.com")
    login_as(admin)

    resp = await client.patch(f"/api/v1/users/{staff.id}", json={"role": "reviewer"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "reviewer"
