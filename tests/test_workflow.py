"""Incident workflow state machine (UC-08; US-10, US-11, NFR-06, NFR-07, NFR-10)."""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest

from app.models.enums import IncidentStatus, Role, UserStatus
from app.services import notification_service
from tests.conftest import (
    count_transitions,
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)

E1_MESSAGE = "This transition is not permitted from the current state."
E2_MESSAGE = "A resolution note is required to close this incident."


class Fixture:
    """One tenant with every role, a category and one incident."""

    def __init__(self, tenant, staff, reviewer, other_reviewer, admin, category, incident):
        self.tenant = tenant
        self.staff = staff
        self.reviewer = reviewer
        self.other_reviewer = other_reviewer
        self.admin = admin
        self.category = category
        self.incident = incident

    @property
    def url(self) -> str:
        return f"/api/v1/incidents/{self.incident.id}/transitions"

    @property
    def assign_url(self) -> str:
        return f"/api/v1/incidents/{self.incident.id}/assign"


async def _setup(
    db,
    *,
    status: IncidentStatus = IncidentStatus.open,
    assign_reviewer: bool = False,
    tenant_name: str = "Acme",
) -> Fixture:
    tenant = await seed_tenant(db, tenant_name)
    staff = await seed_user(db, tenant, Role.staff, email=f"staff@{tenant_name}.test")
    reviewer = await seed_user(db, tenant, Role.reviewer, email=f"rev@{tenant_name}.test")
    other_reviewer = await seed_user(
        db, tenant, Role.reviewer, email=f"rev2@{tenant_name}.test"
    )
    admin = await seed_user(db, tenant, Role.tenant_admin, email=f"admin@{tenant_name}.test")
    category = await seed_category(db, tenant)
    incident = await seed_incident(
        db,
        tenant,
        category=category,
        submitted_by=staff,
        assigned_to=reviewer if assign_reviewer else None,
        status=status,
    )
    return Fixture(tenant, staff, reviewer, other_reviewer, admin, category, incident)


# ---- Valid transitions ----------------------------------------------------------


async def test_open_to_in_review_200(client, db):
    """UC-08 main flow: state changes and exactly one transition row is written."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_review"
    assert len(body["transitions"]) == 1
    assert body["transitions"][0]["from_status"] == "open"
    assert body["transitions"][0]["to_status"] == "in_review"
    assert body["transitions"][0]["transitioned_by"]["id"] == str(fx.reviewer.id)
    assert await count_transitions(db, fx.incident.id) == 1


async def test_open_to_in_review_claims_unassigned_incident(client, db):
    """A reviewer claims work off the shared queue by moving it to In Review."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    body = (await client.post(fx.url, json={"to_status": "in_review"})).json()

    assert body["assigned_to"]["id"] == str(fx.reviewer.id)


async def test_open_to_in_review_does_not_steal_existing_assignee(client, db):
    """Claiming only fills an EMPTY assignee. Driven by the tenant admin, who is the only
    other role that can both see an already-assigned incident and transition it."""
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    body = (await client.post(fx.url, json={"to_status": "in_review"})).json()

    assert body["assigned_to"]["id"] == str(fx.reviewer.id)


async def test_in_review_to_closed_with_note_200(client, db):
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    response = await client.post(
        fx.url, json={"to_status": "closed", "note": "Replaced the fuser unit."}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "closed"
    assert body["transitions"][-1]["note"] == "Replaced the fuser unit."


async def test_transition_response_exposes_updated_allowed_transitions(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)
    body = (await client.post(fx.url, json={"to_status": "in_review"})).json()

    assert body["allowed_transitions"] == ["closed"]


# ---- Invalid transitions (UC-08 E1) ---------------------------------------------


async def test_open_to_closed_returns_409(client, db):
    """The canonical E1 case named in the SRS: Open directly to Closed."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "closed", "note": "skip"})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"] == E1_MESSAGE
    assert error["details"]["reason"] == "invalid_transition"
    assert error["details"]["allowed"] == ["in_review"]


async def test_in_review_to_open_returns_409(client, db):
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)

    assert (await client.post(fx.url, json={"to_status": "open"})).status_code == 409


async def test_closed_to_open_returns_409(client, db):
    fx = await _setup(db, status=IncidentStatus.closed, assign_reviewer=True)

    login_as(fx.reviewer)

    assert (await client.post(fx.url, json={"to_status": "open"})).status_code == 409


async def test_closed_to_in_review_returns_409(client, db):
    """`closed` is terminal — there is no reopen edge in the MVP."""
    fx = await _setup(db, status=IncidentStatus.closed, assign_reviewer=True)

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 409
    assert response.json()["error"]["details"]["allowed"] == []


async def test_open_to_open_returns_409(client, db):
    """from == to, but `open` is never the target of a legal edge, so this is E1 rather
    than an idempotent replay — deterministically, on every attempt."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    first = await client.post(fx.url, json={"to_status": "open"})
    second = await client.post(fx.url, json={"to_status": "open"})

    assert first.status_code == second.status_code == 409
    assert await count_transitions(db, fx.incident.id) == 0


async def test_invalid_transition_leaves_state_and_history_unchanged(client, db):
    """UC-08 E1.2."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "closed", "note": "skip"})
    detail = (await client.get(f"/api/v1/incidents/{fx.incident.id}")).json()

    assert detail["status"] == "open"
    assert detail["transitions"] == []
    assert await count_transitions(db, fx.incident.id) == 0


async def test_unknown_to_status_value_returns_422(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)

    assert (await client.post(fx.url, json={"to_status": "archived"})).status_code == 422


# ---- Resolution note (UC-08 E2) -------------------------------------------------


async def test_close_without_note_returns_422(client, db):
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "closed"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == E2_MESSAGE
    assert error["details"]["reason"] == "resolution_note_required"


async def test_close_with_whitespace_only_note_returns_422(client, db):
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "closed", "note": "   "})

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "resolution_note_required"


async def test_close_with_null_note_returns_422(client, db):
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "closed", "note": None})

    assert response.status_code == 422


async def test_note_is_optional_for_open_to_in_review(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)

    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 200


async def test_failed_close_writes_no_transition_row(client, db):
    """Atomicity: a rejected close must not leave a half-applied audit trail."""
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "closed"})

    assert await count_transitions(db, fx.incident.id) == 0


# ---- Idempotency (NFR-06) -------------------------------------------------------


async def test_repeat_open_to_in_review_is_idempotent(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)
    first = await client.post(fx.url, json={"to_status": "in_review"})
    second = await client.post(fx.url, json={"to_status": "in_review"})

    assert first.status_code == second.status_code == 200
    assert await count_transitions(db, fx.incident.id) == 1


async def test_repeat_close_is_idempotent(client, db):
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    body = {"to_status": "closed", "note": "Resolved."}
    await client.post(fx.url, json=body)
    second = await client.post(fx.url, json=body)

    assert second.status_code == 200
    assert await count_transitions(db, fx.incident.id) == 1


async def test_replayed_close_without_note_returns_200(client, db):
    """E2 guards the *writing* of a transition row. A replay writes nothing, so there is
    nothing to guard — a second close of an already-closed incident is a no-op, not a 422.
    """
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "closed", "note": "Resolved."})
    replay = await client.post(fx.url, json={"to_status": "closed"})

    assert replay.status_code == 200
    assert replay.json()["status"] == "closed"
    assert await count_transitions(db, fx.incident.id) == 1


async def test_replay_returns_identical_body(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)
    first = (await client.post(fx.url, json={"to_status": "in_review"})).json()
    second = (await client.post(fx.url, json={"to_status": "in_review"})).json()

    assert first == second
    # A replay writes nothing, so the row is not touched at all.
    assert first["updated_at"] == second["updated_at"]


async def test_concurrent_identical_transitions_create_one_record(client, db):
    """NFR-06 under real concurrency. The guarded UPDATE carries the expected current
    state in its WHERE clause, so exactly one of the two requests can win; the loser
    re-reads and takes the replay path.

    If this ever flakes under ASGITransport, call workflow_service.transition directly
    from two independent sessions instead — the guarantee under test is in the service.
    """
    fx = await _setup(db)

    login_as(fx.reviewer)
    first, second = await asyncio.gather(
        client.post(fx.url, json={"to_status": "in_review"}),
        client.post(fx.url, json={"to_status": "in_review"}),
    )

    assert {first.status_code, second.status_code} == {200}
    assert await count_transitions(db, fx.incident.id) == 1


# ---- RBAC (US-11, NFR-10) -------------------------------------------------------


async def test_reviewer_can_transition_200(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)

    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 200


async def test_staff_cannot_transition_403(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "insufficient_role"
    assert await count_transitions(db, fx.incident.id) == 0


async def test_tenant_admin_can_transition_200(client, db):
    """Tenant Admin is the tenant's escape hatch when a reviewer is deactivated. UC-08
    names only Reviewer as the actor — open question for the product owner."""
    fx = await _setup(db)

    login_as(fx.admin)

    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 200


async def test_system_admin_cannot_transition_403(client, db):
    """System Admin is a platform actor, not a business actor."""
    fx = await _setup(db)
    root = await seed_user(db, fx.tenant, Role.system_admin, email="root@flowdesk.io")

    login_as(root)

    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 403


async def test_reviewer_cannot_transition_incident_in_another_tenant_404(client, db):
    """NFR-12: 404, not 403 — existence is not leaked."""
    fx = await _setup(db)
    other = await _setup(db, tenant_name="Globex")

    login_as(other.reviewer)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 404


async def test_reviewer_cannot_transition_incident_assigned_to_another_reviewer(client, db):
    """Visibility is evaluated before state: an incident outside the caller's scope is 404
    regardless of whether the transition itself would have been legal."""
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.other_reviewer)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 404


async def test_staff_sees_no_allowed_transitions(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    body = (await client.get(f"/api/v1/incidents/{fx.incident.id}")).json()

    assert body["allowed_transitions"] == []


async def test_closed_incident_exposes_no_allowed_transitions(client, db):
    fx = await _setup(db, status=IncidentStatus.closed, assign_reviewer=True)

    login_as(fx.reviewer)
    body = (await client.get(f"/api/v1/incidents/{fx.incident.id}")).json()

    assert body["allowed_transitions"] == []


# ---- Reassignment (UC-08 A1) ----------------------------------------------------


async def test_tenant_admin_reassigns_to_another_reviewer_200(client, db):
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    response = await client.post(
        fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
    )

    assert response.status_code == 200
    assert response.json()["assigned_to"]["id"] == str(fx.other_reviewer.id)


async def test_reassign_to_staff_returns_422(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.post(fx.assign_url, json={"assigned_to": str(fx.staff.id)})

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "invalid_assignee"


async def test_reassign_to_user_in_another_tenant_returns_422(client, db):
    """Same message as every other invalid-assignee case, so nothing leaks."""
    fx = await _setup(db)
    other = await _setup(db, tenant_name="Globex")

    login_as(fx.admin)
    foreign = await client.post(
        fx.assign_url, json={"assigned_to": str(other.reviewer.id)}
    )
    unknown = await client.post(fx.assign_url, json={"assigned_to": str(uuid.uuid4())})

    assert foreign.status_code == unknown.status_code == 422
    assert foreign.json()["error"] == unknown.json()["error"]


async def test_reassign_to_inactive_reviewer_returns_422(client, db):
    fx = await _setup(db)
    dormant = await seed_user(
        db, fx.tenant, Role.reviewer, email="dormant@acme.test", status=UserStatus.inactive
    )

    login_as(fx.admin)
    response = await client.post(fx.assign_url, json={"assigned_to": str(dormant.id)})

    assert response.status_code == 422


async def test_reassign_closed_incident_returns_409(client, db):
    fx = await _setup(db, status=IncidentStatus.closed, assign_reviewer=True)

    login_as(fx.admin)
    response = await client.post(
        fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["reason"] == "incident_closed"


async def test_reassign_writes_no_workflow_transition_row(client, db):
    """workflow_transitions records state changes only; from_status/to_status are NOT
    NULL, so a reassignment has no honest row to write there."""
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    await client.post(fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)})

    assert await count_transitions(db, fx.incident.id) == 0


async def test_reviewer_cannot_reassign_403(client, db):
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.reviewer)
    response = await client.post(
        fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
    )

    assert response.status_code == 403


async def test_staff_cannot_reassign_403(client, db):
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.staff)
    response = await client.post(
        fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
    )

    assert response.status_code == 403


async def test_reassign_moves_visibility_between_reviewers(client, db):
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    await client.post(fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)})

    login_as(fx.other_reviewer)
    assert (await client.get(f"/api/v1/incidents/{fx.incident.id}")).status_code == 200

    login_as(fx.reviewer)
    assert (await client.get(f"/api/v1/incidents/{fx.incident.id}")).status_code == 404


# ---- Audit and the Sprint-3 notification seam (NFR-07) --------------------------


async def test_transition_is_audit_logged(client, db, caplog):
    """NFR-07: every workflow transition is recorded for audit."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    with caplog.at_level(logging.INFO, logger="flowdesk.audit"):
        await client.post(fx.url, json={"to_status": "in_review"})

    records = [r for r in caplog.records if "workflow_transition" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert str(fx.incident.id) in message
    assert str(fx.reviewer.id) in message
    assert "from=open" in message and "to=in_review" in message


async def test_replayed_transition_is_not_double_logged(client, db, caplog):
    """A replay changes nothing, so there is no durable fact to audit."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    with caplog.at_level(logging.INFO, logger="flowdesk.audit"):
        await client.post(fx.url, json={"to_status": "in_review"})
        await client.post(fx.url, json={"to_status": "in_review"})

    records = [r for r in caplog.records if "workflow_transition" in r.getMessage()]
    assert len(records) == 1


async def test_reassignment_is_audit_logged(client, db, caplog):
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    with caplog.at_level(logging.INFO, logger="flowdesk.audit"):
        await client.post(
            fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
        )

    assert any("incident_reassign" in r.getMessage() for r in caplog.records)


async def test_transition_invokes_notification_hook_once(client, db, monkeypatch):
    """UC-08 step 9 has a connection point for Sprint 3 (US-13)."""
    fx = await _setup(db)
    calls: list[dict] = []

    async def _spy(db_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        notification_service, "notify_incident_transitioned", _spy
    )

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})

    assert len(calls) == 1
    assert calls[0]["from_status"] is IncidentStatus.open
    assert calls[0]["to_status"] is IncidentStatus.in_review
    assert calls[0]["actor"].id == fx.reviewer.id


@pytest.mark.parametrize(
    ("setup_status", "body"),
    [
        # Replay: already in the target state.
        (IncidentStatus.in_review, {"to_status": "in_review"}),
        # UC-08 E1: illegal jump.
        (IncidentStatus.open, {"to_status": "closed", "note": "skip"}),
        # UC-08 E2: missing resolution note.
        (IncidentStatus.in_review, {"to_status": "closed"}),
    ],
)
async def test_notification_hook_not_called_without_a_state_change(
    client, db, monkeypatch, setup_status, body
):
    """Only a real state change fires the hook — this protects the Sprint-3 contract."""
    fx = await _setup(db, status=setup_status, assign_reviewer=True)
    calls: list[dict] = []

    async def _spy(db_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(notification_service, "notify_incident_transitioned", _spy)

    login_as(fx.reviewer)
    await client.post(fx.url, json=body)

    assert calls == []
