"""Every error carries a machine-readable `details.reason` (D-5, D-12).

Six errors shipped with `details: {}`, so the only way a client could tell them apart was
to match on the human message string — text that exists to be reworded, and in two cases
was already reworded once. Two of them share a message *and* an endpoint, and two 404s that
mean completely different things ("no such URL" and "that record is not yours") were
indistinguishable: same `code`, same empty `details`.

The assertions here are deliberately about `details.reason` and not about the message. A
message change should not break a test; a slug change is a contract break and should.

`test_route_404_is_distinguishable_from_a_scope_404` is the one worth reading twice — it is
the pair, not either half, that was the defect.
"""

from __future__ import annotations

import uuid

from app.api import deps
from app.main import app as fastapi_app
from app.models.enums import Role, UserStatus
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)

_ORG_PAYLOAD = {
    "organization_name": "Acme",
    "admin_email": "admin@acme.com",
    "admin_name": "Admin",
}


def _reason(response) -> str | None:
    return response.json()["error"]["details"].get("reason")


# ---- 404s -------------------------------------------------------------------------


async def test_unrouted_path_carries_route_not_found(anon_client):
    """No database, no auth: this 404 is Starlette's, raised before any handler runs."""
    response = await anon_client.get("/api/v1/no-such-collection")

    assert response.status_code == 404
    assert _reason(response) == "route_not_found"


async def test_user_not_found_carries_a_reason(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    response = await client.get(f"/api/v1/users/{uuid.uuid4()}")

    assert response.status_code == 404
    assert _reason(response) == "user_not_found"


async def test_category_not_found_carries_a_reason(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    response = await client.get(f"/api/v1/categories/{uuid.uuid4()}")

    assert response.status_code == 404
    assert _reason(response) == "category_not_found"


async def test_incident_not_found_carries_a_reason(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    response = await client.get(f"/api/v1/incidents/{uuid.uuid4()}")

    assert response.status_code == 404
    assert _reason(response) == "incident_not_found"


async def test_route_404_is_distinguishable_from_a_scope_404(client, db):
    """The point of the exercise.

    Both answers are `404 not_found` with the same shape. Before D-5 the only thing that
    differed was the prose, so a client could not tell "you typed the wrong URL" from
    "that record belongs to another organisation" — and the second is a tenant-isolation
    refusal that a support engineer very much wants to recognise.
    """
    tenant = await seed_tenant(db, "Acme")
    other = await seed_tenant(db, "Other")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    foreign = await seed_category(db, other)
    login_as(admin)

    routing = await client.get("/api/v1/categories-typo")
    scoped = await client.get(f"/api/v1/categories/{foreign.id}")

    assert routing.status_code == scoped.status_code == 404
    assert routing.json()["error"]["code"] == scoped.json()["error"]["code"]
    assert _reason(routing) == "route_not_found"
    assert _reason(scoped) == "category_not_found"


# ---- 409s -------------------------------------------------------------------------


async def test_duplicate_category_name_carries_a_reason(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    await seed_category(db, tenant, name="Facilities")
    login_as(admin)

    response = await client.post("/api/v1/categories", json={"name": "Facilities"})

    assert response.status_code == 409
    assert _reason(response) == "category_name_taken"


async def test_duplicate_email_in_tenant_carries_a_reason(client, db, fake_supabase):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin, email="dup@acme.com")
    login_as(admin)

    response = await client.post(
        "/api/v1/users",
        json={"email": "dup@acme.com", "name": "Dup", "role": "staff"},
    )

    assert response.status_code == 409
    assert _reason(response) == "user_email_taken"


async def test_email_registered_elsewhere_carries_a_reason(client, db, fake_supabase):
    """Same status, same endpoint, different cause — and previously the same empty details.

    The address is free inside Acme but already belongs to a FlowDesk user in another
    organisation, so the in-tenant check passes and Supabase is the one that refuses. A
    tenant admin can act on `user_email_taken` (pick another address, or find the existing
    account); `email_registered` they cannot see or fix at all, which is why the two must
    not look identical to the client.
    """
    acme = await seed_tenant(db, "Acme")
    other = await seed_tenant(db, "Other")
    admin = await seed_user(db, acme, Role.tenant_admin)
    squatter = await seed_user(db, other, Role.staff, email="taken@example.com")
    fake_supabase.existing_emails.add("taken@example.com")
    fake_supabase.seed_auth_user("taken@example.com", squatter.id)
    login_as(admin)

    response = await client.post(
        "/api/v1/users",
        json={"email": "taken@example.com", "name": "Taken", "role": "staff"},
    )

    assert response.status_code == 409
    assert _reason(response) == "email_registered"


async def test_duplicate_organisation_name_carries_a_reason(client, db, fake_supabase):
    # `client`, not `anon_client`: registration is public but it does reach the database,
    # and `anon_client` leaves `get_db` un-overridden on purpose so that a test which
    # touches it fails loudly rather than quietly using the wrong engine.
    await seed_tenant(db, "Acme")

    # Lower-cased: the pre-check is case-insensitive, and this proves the slug is attached
    # to that branch and not only to the IntegrityError one behind it.
    response = await client.post(
        "/api/v1/organizations", json={**_ORG_PAYLOAD, "organization_name": "acme"}
    )

    assert response.status_code == 409
    assert _reason(response) == "organization_name_taken"


async def test_registering_with_a_known_email_carries_a_reason(
    client, db, fake_supabase
):
    fake_supabase.existing_emails.add(_ORG_PAYLOAD["admin_email"])

    response = await client.post("/api/v1/organizations", json=_ORG_PAYLOAD)

    assert response.status_code == 409
    assert _reason(response) == "email_registered"


# ---- 401 (D-12) -------------------------------------------------------------------


async def test_me_with_a_dangling_tenant_carries_a_reason(client, db):
    """D-12: the only 401 in the API that shipped without a slug.

    A token can be perfectly valid and still land here, so a client that force-logs-out on
    every unslugged 401 would do the wrong thing. The state is unreachable through the API
    — `users.tenant_id` is a foreign key — so the caller is injected rather than seeded.
    """
    orphan = deps.CurrentUser(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),  # no tenants row will ever match
        email="orphan@example.com",
        name="Orphan",
        role=Role.staff,
        status=UserStatus.active,
    )
    fastapi_app.dependency_overrides[deps.get_current_user] = lambda: orphan

    response = await client.get("/api/v1/me")

    assert response.status_code == 401
    assert _reason(response) == "tenant_not_found"


# ---- 405 (D-8) --------------------------------------------------------------------


async def test_405_lists_the_permitted_methods(anon_client):
    """D-8. Starlette attaches `Allow` to the 405 it raises; the envelope handler used to
    drop every header on the way out, so the API told a client its request was wrong
    without ever saying what would have been right."""
    response = await anon_client.post("/api/v1/notifications")

    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


async def test_405_still_renders_the_envelope(anon_client):
    """Restoring the header must not cost the body — both, or the fix is a regression."""
    response = await anon_client.delete("/api/v1/notifications/unread-count")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert "allow" in response.headers


# ---- The tenant that owns the record (D-7) ----------------------------------------


async def test_incident_rows_carry_their_tenant(client, db):
    """D-7: a System Admin's list spans organisations and could not be grouped by one."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    category = await seed_category(db, tenant)
    await seed_incident(db, tenant, category=category, submitted_by=admin)
    login_as(admin)

    response = await client.get("/api/v1/incidents")

    assert response.status_code == 200
    assert response.json()["data"][0]["tenant_id"] == str(tenant.id)


async def test_user_rows_carry_their_tenant(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    login_as(admin)

    response = await client.get("/api/v1/users")

    assert response.status_code == 200
    assert response.json()["data"][0]["tenant_id"] == str(tenant.id)
