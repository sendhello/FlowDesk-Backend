"""Cross-tenant isolation (NFR-12). A tenant admin must never reach another tenant's
data: cross-tenant access returns 404 (existence is not leaked)."""

from __future__ import annotations

from app.models.enums import IncidentStatus, Role
from tests.conftest import (
    count_transitions,
    login_as,
    seed_category,
    seed_incident,
    seed_notification,
    seed_tenant,
    seed_user,
)


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


async def test_cross_tenant_incident_access_returns_404(client, db):
    """An incident, its detail and its timeline are all unreachable across tenants."""
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    admin_a = await seed_user(db, tenant_a, Role.tenant_admin, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="s@globex.com")
    category_b = await seed_category(db, tenant_b)
    incident_b = await seed_incident(
        db, tenant_b, category=category_b, submitted_by=staff_b
    )

    login_as(admin_a)
    assert (await client.get(f"/api/v1/incidents/{incident_b.id}")).status_code == 404
    assert (
        await client.get(f"/api/v1/incidents/{incident_b.id}/transitions")
    ).status_code == 404
    listed = await client.get("/api/v1/incidents")
    assert listed.json()["pagination"]["total"] == 0


async def test_cross_tenant_transition_returns_404_and_changes_nothing(client, db):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    reviewer_a = await seed_user(db, tenant_a, Role.reviewer, email="r@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="s@globex.com")
    incident_b = await seed_incident(
        db, tenant_b, category=await seed_category(db, tenant_b), submitted_by=staff_b
    )

    login_as(reviewer_a)
    response = await client.post(
        f"/api/v1/incidents/{incident_b.id}/transitions",
        json={"to_status": "in_review"},
    )

    assert response.status_code == 404
    assert await count_transitions(db, incident_b.id) == 0
    await db.refresh(incident_b)
    assert incident_b.status is IncidentStatus.open


async def test_cross_tenant_notification_mark_read_returns_404(client, db):
    """A notification is personal correspondence: not even a System Admin reaches someone
    else's, and out of scope is 404 rather than 403 (there is no role gate to produce one).
    """
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    admin_a = await seed_user(db, tenant_a, Role.tenant_admin, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="s@globex.com")
    incident_b = await seed_incident(
        db, tenant_b, category=await seed_category(db, tenant_b), submitted_by=staff_b
    )
    notification_b = await seed_notification(
        db, tenant_b, user=staff_b, incident=incident_b
    )

    login_as(admin_a)
    response = await client.post(f"/api/v1/notifications/{notification_b.id}/read")

    assert response.status_code == 404
    assert response.json()["error"]["details"]["reason"] == "notification_not_found"


async def test_cross_tenant_notification_list_is_empty(client, db):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    admin_a = await seed_user(db, tenant_a, Role.tenant_admin, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="s@globex.com")
    incident_b = await seed_incident(
        db, tenant_b, category=await seed_category(db, tenant_b), submitted_by=staff_b
    )
    await seed_notification(db, tenant_b, user=staff_b, incident=incident_b)

    login_as(admin_a)
    listed = await client.get("/api/v1/notifications")

    assert listed.json()["pagination"]["total"] == 0


async def test_incident_cannot_use_another_tenants_category(client, db):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    staff_a = await seed_user(db, tenant_a, Role.staff, email="s@acme.com")
    category_b = await seed_category(db, tenant_b, name="B-only")

    login_as(staff_a)
    response = await client.post(
        "/api/v1/incidents",
        json={
            "title": "Cross-tenant attempt",
            "description": "Should be rejected.",
            "category_id": str(category_b.id),
            "severity": "low",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "category_not_in_tenant"
