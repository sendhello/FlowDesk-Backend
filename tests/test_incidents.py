"""Incident submission and detail (UC-06, UC-11; US-08, US-12)."""

from __future__ import annotations

import uuid

from app.models.enums import IncidentStatus, Role, Severity
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_transition,
    seed_user,
)


def _payload(category, **overrides) -> dict:
    body = {
        "title": "Printer on fire",
        "description": "The third-floor printer is emitting smoke.",
        "category_id": str(category.id),
        "severity": "high",
    }
    body.update(overrides)
    return body


async def test_staff_submits_incident_201(client, db):
    """UC-06 main flow: status, submitter and tenant are set by the server."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant, name="Hardware")

    login_as(staff)
    response = await client.post("/api/v1/incidents", json=_payload(category))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "open"
    assert body["severity"] == "high"
    assert body["submitted_by"]["id"] == str(staff.id)
    assert body["assigned_to"] is None
    assert body["category"]["id"] == str(category.id)
    # UC-08 postcondition has not run yet, and there is no synthetic "created" event.
    assert body["transitions"] == []
    # Staff never transitions, so the frontend renders no workflow buttons.
    assert body["allowed_transitions"] == []


async def test_submit_missing_title_returns_422(client, db):
    """UC-06 E1: a required field is missing, so no incident is created."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    body = _payload(category)
    del body["title"]
    response = await client.post("/api/v1/incidents", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_submit_missing_description_returns_422(client, db):
    """UC-06 E1."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    response = await client.post(
        "/api/v1/incidents", json=_payload(category, description="")
    )

    assert response.status_code == 422


async def test_submit_title_at_max_length_accepted(client, db):
    """Boundary: title of exactly 255 characters is valid."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    response = await client.post(
        "/api/v1/incidents", json=_payload(category, title="x" * 255)
    )

    assert response.status_code == 201


async def test_submit_title_over_max_length_returns_422(client, db):
    """Boundary: 256 characters exceeds the column width."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    response = await client.post(
        "/api/v1/incidents", json=_payload(category, title="x" * 256)
    )

    assert response.status_code == 422


async def test_submit_invalid_severity_returns_422(client, db):
    """Severity is a closed enum; FastAPI rejects anything else natively."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    response = await client.post(
        "/api/v1/incidents", json=_payload(category, severity="catastrophic")
    )

    assert response.status_code == 422


async def test_submit_unknown_category_returns_422(client, db):
    """The category is a body field, not the addressed resource, so 422 rather than 404."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    response = await client.post(
        "/api/v1/incidents", json=_payload(category, category_id=str(uuid.uuid4()))
    )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "category_not_in_tenant"


async def test_submit_category_from_another_tenant_returns_422(client, db):
    """NFR-12: identical slug and message to the unknown-category case — no leak."""
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    staff_a = await seed_user(db, tenant_a, Role.staff)
    own_category = await seed_category(db, tenant_a)
    foreign_category = await seed_category(db, tenant_b, name="B-only")

    login_as(staff_a)
    unknown = await client.post(
        "/api/v1/incidents", json=_payload(own_category, category_id=str(uuid.uuid4()))
    )
    foreign = await client.post(
        "/api/v1/incidents",
        json=_payload(own_category, category_id=str(foreign_category.id)),
    )

    assert foreign.status_code == unknown.status_code == 422
    assert foreign.json()["error"] == unknown.json()["error"]


async def test_reviewer_cannot_submit_403(client, db):
    """UC-06 lists Staff as the actor. Open question for the product owner: should
    reviewers and tenant admins be able to raise incidents too?"""
    tenant = await seed_tenant(db, "Acme")
    reviewer = await seed_user(db, tenant, Role.reviewer)
    category = await seed_category(db, tenant)

    login_as(reviewer)
    response = await client.post("/api/v1/incidents", json=_payload(category))

    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "insufficient_role"


async def test_tenant_admin_cannot_submit_403(client, db):
    """UC-06 actor is Staff — see the open question on the reviewer case."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    category = await seed_category(db, tenant)

    login_as(admin)
    response = await client.post("/api/v1/incidents", json=_payload(category))

    assert response.status_code == 403


async def test_incident_detail_returns_full_record_and_empty_timeline(client, db):
    """UC-11: a freshly submitted incident has no transitions — the frontend renders the
    creation entry from created_at / submitted_by."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant, name="Network")
    incident = await seed_incident(db, tenant, category=category, submitted_by=staff)

    login_as(staff)
    response = await client.get(f"/api/v1/incidents/{incident.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Something broke."
    assert body["category"]["name"] == "Network"
    assert body["submitted_by"]["email"] == staff.email
    assert body["transitions"] == []


async def test_incident_detail_includes_history_in_chronological_order(client, db):
    """UC-11 step 3: the timeline is ordered oldest first."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    reviewer = await seed_user(db, tenant, Role.reviewer)
    category = await seed_category(db, tenant)
    incident = await seed_incident(
        db,
        tenant,
        category=category,
        submitted_by=staff,
        assigned_to=reviewer,
        status=IncidentStatus.closed,
    )
    await seed_transition(
        db,
        incident,
        from_status=IncidentStatus.open,
        to_status=IncidentStatus.in_review,
        by=reviewer,
    )
    await seed_transition(
        db,
        incident,
        from_status=IncidentStatus.in_review,
        to_status=IncidentStatus.closed,
        by=reviewer,
        note="Replaced the fuser unit.",
    )

    login_as(staff)
    body = (await client.get(f"/api/v1/incidents/{incident.id}")).json()

    assert [t["to_status"] for t in body["transitions"]] == ["in_review", "closed"]
    assert body["transitions"][1]["note"] == "Replaced the fuser unit."
    assert body["transitions"][1]["transitioned_by"]["id"] == str(reviewer.id)
    timestamps = [t["created_at"] for t in body["transitions"]]
    assert timestamps == sorted(timestamps)


async def test_incident_detail_unknown_id_returns_404(client, db):
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)

    login_as(staff)
    response = await client.get(f"/api/v1/incidents/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_staff_cannot_see_another_staffs_incident_404(client, db):
    """Same tenant, different submitter: 404 rather than 403, so existence is not leaked."""
    tenant = await seed_tenant(db, "Acme")
    author = await seed_user(db, tenant, Role.staff, email="author@acme.com")
    other = await seed_user(db, tenant, Role.staff, email="other@acme.com")
    category = await seed_category(db, tenant)
    incident = await seed_incident(db, tenant, category=category, submitted_by=author)

    login_as(other)
    response = await client.get(f"/api/v1/incidents/{incident.id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_tenant_admin_sees_any_incident_in_tenant_200(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    incident = await seed_incident(db, tenant, category=category, submitted_by=staff)

    login_as(admin)
    response = await client.get(f"/api/v1/incidents/{incident.id}")

    assert response.status_code == 200
    # A tenant admin may transition, so the workflow buttons are offered.
    assert response.json()["allowed_transitions"] == ["in_review"]


async def test_submitter_keeps_visibility_after_incident_is_assigned_away(client, db):
    """Staff scope is submitted_by, so assignment to a reviewer does not hide it."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    reviewer = await seed_user(db, tenant, Role.reviewer)
    category = await seed_category(db, tenant)
    incident = await seed_incident(
        db, tenant, category=category, submitted_by=staff, assigned_to=reviewer
    )

    login_as(staff)
    response = await client.get(f"/api/v1/incidents/{incident.id}")

    assert response.status_code == 200
    assert response.json()["assigned_to"]["id"] == str(reviewer.id)


async def test_severity_round_trips_all_values(client, db):
    """All four UC-06 severity levels are accepted and echoed back."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)

    login_as(staff)
    for severity in Severity:
        response = await client.post(
            "/api/v1/incidents", json=_payload(category, severity=severity.value)
        )
        assert response.status_code == 201
        assert response.json()["severity"] == severity.value
