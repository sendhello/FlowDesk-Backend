"""Tenant workspace settings (D-18).

The defect these close is worth restating, because it shapes what is asserted here. The
settings page had no backend at all: no router, no model, no column. Clicking Save issued
no request and the page reported success anyway, so a user believed a change had been
saved that had never left the browser — `tc01_before.png` and `tc01_after.png` are
byte-identical.

"The response says 200" is therefore not enough for this feature specifically. Every
mutation test below re-reads the record afterwards, because a confirmation that is not
backed by storage is precisely the failure being fixed.
"""

from __future__ import annotations

import pytest

from app.models.enums import Role
from app.models.tenant import DEFAULT_TENANT_TIMEZONE
from tests.conftest import login_as, seed_tenant, seed_user

WRITE_ROLES = {Role.tenant_admin}


async def _admin_of(db, name: str = "Demo Organisation"):
    tenant = await seed_tenant(db, name)
    admin = await seed_user(db, tenant, Role.tenant_admin, email=f"admin@{name[:4]}.test")
    login_as(admin)
    return tenant, admin


# ---- Read -------------------------------------------------------------------------


async def test_read_returns_the_callers_own_organisation(client, db):
    tenant, _ = await _admin_of(db)

    response = await client.get("/api/v1/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(tenant.id)
    assert body["name"] == "Demo Organisation"


async def test_a_new_tenant_starts_on_the_platform_timezone(client, db):
    """The column is NOT NULL and backfilled, so the screen always has a value to show —
    and it is the zone the analytics were already using, so introducing the setting moved
    nobody's weekly buckets."""
    await _admin_of(db)

    response = await client.get("/api/v1/settings")

    assert response.json()["timezone"] == DEFAULT_TENANT_TIMEZONE


# ---- Write ------------------------------------------------------------------------


async def test_renaming_the_organisation_persists(client, db):
    await _admin_of(db)

    saved = await client.patch("/api/v1/settings", json={"name": "North"})
    reread = await client.get("/api/v1/settings")

    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "North"
    assert reread.json()["name"] == "North"


async def test_changing_the_timezone_persists(client, db):
    await _admin_of(db)

    saved = await client.patch(
        "/api/v1/settings", json={"timezone": "Australia/Perth"}
    )
    reread = await client.get("/api/v1/settings")

    assert saved.status_code == 200, saved.text
    assert reread.json()["timezone"] == "Australia/Perth"


async def test_both_fields_change_together(client, db):
    """TC-01 verbatim: the case edited the organisation name and the timezone in one
    submission, and neither took effect."""
    await _admin_of(db)

    saved = await client.patch(
        "/api/v1/settings",
        json={"name": "North", "timezone": "Australia/Melbourne"},
    )

    assert saved.status_code == 200, saved.text
    reread = (await client.get("/api/v1/settings")).json()
    assert (reread["name"], reread["timezone"]) == ("North", "Australia/Melbourne")


async def test_omitted_fields_are_left_alone(client, db):
    await _admin_of(db)
    await client.patch("/api/v1/settings", json={"timezone": "Australia/Perth"})

    await client.patch("/api/v1/settings", json={"name": "North"})

    reread = (await client.get("/api/v1/settings")).json()
    assert reread["name"] == "North"
    assert reread["timezone"] == "Australia/Perth"


async def test_saving_advances_updated_at(client, db):
    """UC-01 step 5 shows a confirmation. `updated_at` is what makes that confirmation
    checkable after the fact rather than a claim made by the page."""
    await _admin_of(db)
    before = (await client.get("/api/v1/settings")).json()["updated_at"]

    await client.patch("/api/v1/settings", json={"name": "North"})

    after = (await client.get("/api/v1/settings")).json()["updated_at"]
    assert after > before


# ---- Validation and conflict ------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["Mars/Olympus_Mons", "GMT+11", "australia/melbourne ", ""]
)
async def test_an_unknown_timezone_is_refused(client, db, value):
    """UC-01 E1. Storing a bad zone would not fail here — it would fail later, on every
    analytics query, on a page the admin was not looking at."""
    await _admin_of(db)

    response = await client.patch("/api/v1/settings", json={"timezone": value})

    assert response.status_code == 422, response.text


async def test_an_unknown_timezone_carries_a_reason(client, db):
    await _admin_of(db)

    response = await client.patch(
        "/api/v1/settings", json={"timezone": "Mars/Olympus_Mons"}
    )

    assert response.json()["error"]["details"]["reason"] == "invalid_timezone"


async def test_a_refused_save_changes_nothing(client, db):
    """UC-01 E1.2: changes are not saved. Both fields arrive in one request and one of
    them is invalid, so the valid half must not land either."""
    await _admin_of(db)

    await client.patch(
        "/api/v1/settings", json={"name": "North", "timezone": "Nowhere/Nothing"}
    )

    reread = (await client.get("/api/v1/settings")).json()
    assert reread["name"] == "Demo Organisation"
    assert reread["timezone"] == DEFAULT_TENANT_TIMEZONE


async def test_renaming_onto_another_organisation_is_refused(client, db):
    await seed_tenant(db, "River")
    await _admin_of(db)

    response = await client.patch("/api/v1/settings", json={"name": "River"})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["details"]["reason"] == "organization_name_taken"


async def test_the_name_clash_check_ignores_case(client, db):
    """`uq_tenants_name` compares exactly, so the database would accept "river" happily.
    To a human that is the same organisation, which is why registration pre-checks
    case-insensitively — and a rename is the same collision through a different door."""
    await seed_tenant(db, "River")
    await _admin_of(db)

    response = await client.patch("/api/v1/settings", json={"name": "river"})

    assert response.status_code == 409, response.text


async def test_renaming_to_the_current_name_is_allowed(client, db):
    """The self-collision trap: an idempotent save must not be refused by its own row."""
    await _admin_of(db)

    response = await client.patch(
        "/api/v1/settings", json={"name": "Demo Organisation"}
    )

    assert response.status_code == 200, response.text


async def test_a_one_character_name_is_refused(client, db):
    await _admin_of(db)

    response = await client.patch("/api/v1/settings", json={"name": "A"})

    assert response.status_code == 422, response.text


# ---- Access control ---------------------------------------------------------------


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
async def test_read_permission_matrix(client, db, role):
    """TC-01's second half asks that a caller without the right be denied.

    Note for the test plan: the case as written names Tenant Admin as the denied actor,
    because it was written against UC-01's *platform* settings where System Admin is the
    owner. These are tenant settings, so Tenant Admin is the permitted actor and the
    denial falls on the other three roles. TC-01 needs rewording; the behaviour here is
    the behaviour the screen requires.
    """
    tenant = await seed_tenant(db, "Acme")
    actor = await seed_user(db, tenant, role, email=f"{role.value}@acme.test")
    login_as(actor)

    response = await client.get("/api/v1/settings")

    if role in WRITE_ROLES:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["details"]["reason"] == "insufficient_role"


@pytest.mark.parametrize("role", list(Role), ids=lambda r: r.value)
async def test_write_permission_matrix(client, db, role):
    tenant = await seed_tenant(db, "Acme")
    actor = await seed_user(db, tenant, role, email=f"{role.value}@acme.test")
    login_as(actor)

    response = await client.patch("/api/v1/settings", json={"name": "Renamed"})

    if role in WRITE_ROLES:
        assert response.status_code == 200, response.text
    else:
        assert response.status_code == 403, response.text


async def test_a_denied_caller_learns_nothing_about_the_settings(client, db):
    """"Denied without the resource being revealed", per TC-01. The 403 body must not
    leak the organisation name or its timezone."""
    tenant = await seed_tenant(db, "Confidential Holdings")
    staff = await seed_user(db, tenant, Role.staff, email="staff@acme.test")
    login_as(staff)

    response = await client.get("/api/v1/settings")

    assert response.status_code == 403
    assert "Confidential Holdings" not in response.text


async def test_an_admin_cannot_edit_another_organisation(client, db):
    """NFR-12. There is no tenant id in the path, so the isolation guarantee is that the
    route reads the caller's own scope and offers no way to name a different one."""
    other = await seed_tenant(db, "River")
    await _admin_of(db, "North")

    await client.patch("/api/v1/settings", json={"name": "North Renamed"})

    await db.refresh(other)
    assert other.name == "River"
