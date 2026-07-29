"""Notification dispatch (UC-08 step 9, UC-09 steps 1-2, UC-09 E1; US-13).

Covers what the system WRITES when an incident moves. The panel that reads those rows is
tests/test_notifications_panel.py.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import select

from app.models.enums import IncidentStatus, Role, UserStatus
from app.models.notification import Notification
from app.services import notification_service
from tests.conftest import (
    as_current_user,
    count_notifications,
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)


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
    title: str = "Test incident",
) -> Fixture:
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff, email="staff@acme.test")
    reviewer = await seed_user(db, tenant, Role.reviewer, email="rev@acme.test")
    other_reviewer = await seed_user(db, tenant, Role.reviewer, email="rev2@acme.test")
    admin = await seed_user(db, tenant, Role.tenant_admin, email="admin@acme.test")
    category = await seed_category(db, tenant)
    incident = await seed_incident(
        db,
        tenant,
        category=category,
        submitted_by=staff,
        assigned_to=reviewer if assign_reviewer else None,
        status=status,
        title=title,
    )
    return Fixture(tenant, staff, reviewer, other_reviewer, admin, category, incident)


async def _notifications_for(db, user) -> list[Notification]:
    rows = await db.scalars(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at, Notification.id)
    )
    return list(rows)


# ---- A real transition notifies the submitter (UC-08 step 9) --------------------


async def test_transition_notifies_submitter(client, db):
    """Given an open incident, when a reviewer moves it to In Review, then its submitter
    receives exactly one notification."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 200

    assert await count_notifications(db, fx.staff.id) == 1


async def test_notification_message_contains_title_and_new_state(client, db):
    """UC-09 step 2. The state is rendered for humans ("In Review"), not as the enum
    value ("in_review")."""
    fx = await _setup(db, title="Printer on fire")

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})

    (notification,) = await _notifications_for(db, fx.staff)
    assert "Printer on fire" in notification.message
    assert "In Review" in notification.message
    assert "in_review" not in notification.message


async def test_notification_links_to_the_incident(client, db):
    """UC-09 step 7 navigates to the incident detail page, so incident_id must be right."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})

    (notification,) = await _notifications_for(db, fx.staff)
    assert notification.incident_id == fx.incident.id


async def test_notification_starts_unread(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})

    (notification,) = await _notifications_for(db, fx.staff)
    assert notification.is_read is False


async def test_notification_tenant_matches_recipient_tenant(client, db):
    """tenant_id is written from the incident, never queried. It must still be correct."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})

    (notification,) = await _notifications_for(db, fx.staff)
    assert notification.tenant_id == fx.tenant.id == fx.staff.tenant_id


async def test_notification_is_committed_with_the_transition(client, db):
    """Both are durable after one request: delivery happens inside the transition's
    transaction, so a caller that sees the new status also sees the notification.

    Both assertions read from the database on a session that took no part in the request.
    """
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})

    incident = (await client.get(f"/api/v1/incidents/{fx.incident.id}")).json()
    assert incident["status"] == "in_review"
    assert await count_notifications(db, fx.staff.id) == 1


# ---- No state change means no notification --------------------------------------


async def test_replayed_transition_creates_no_notification(client, db):
    """NFR-06. The second call writes no row, so it must not notify again either."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(fx.url, json={"to_status": "in_review"})
    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 200

    assert await count_notifications(db, fx.staff.id) == 1


async def test_invalid_transition_creates_no_notification(client, db):
    """UC-08 E1: open -> closed is rejected, so nobody is told anything happened."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    assert (await client.post(fx.url, json={"to_status": "closed"})).status_code == 409

    assert await count_notifications(db, fx.staff.id) == 0


async def test_failed_close_creates_no_notification(client, db):
    """UC-08 E2: closing without a note is rejected atomically — no row, no notification."""
    fx = await _setup(db, status=IncidentStatus.in_review, assign_reviewer=True)

    login_as(fx.reviewer)
    assert (await client.post(fx.url, json={"to_status": "closed"})).status_code == 422

    assert await count_notifications(db, fx.staff.id) == 0


# ---- Reassignment (UC-08 A1.2) --------------------------------------------------


async def test_reassign_notifies_new_reviewer(client, db):
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    response = await client.post(
        fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
    )

    assert response.status_code == 200
    assert await count_notifications(db, fx.other_reviewer.id) == 1


async def test_reassign_does_not_notify_previous_reviewer(client, db):
    """The SRS does not ask for it, and it tells the previous reviewer nothing
    actionable."""
    fx = await _setup(db, assign_reviewer=True)

    login_as(fx.admin)
    await client.post(fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)})

    assert await count_notifications(db, fx.reviewer.id) == 0


async def test_reassign_message_names_the_incident(client, db):
    fx = await _setup(db, assign_reviewer=True, title="Printer on fire")

    login_as(fx.admin)
    await client.post(fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)})

    (notification,) = await _notifications_for(db, fx.other_reviewer)
    assert "Printer on fire" in notification.message
    assert notification.incident_id == fx.incident.id


async def test_failed_reassign_creates_no_notification(client, db):
    """An invalid assignee (422 invalid_assignee) writes nothing at all."""
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.post(fx.assign_url, json={"assigned_to": str(fx.staff.id)})

    assert response.status_code == 422
    assert await count_notifications(db, fx.staff.id) == 0


async def test_reassign_closed_incident_creates_no_notification(client, db):
    """UC-08 A1: a closed incident is 409 incident_closed, before any delivery."""
    fx = await _setup(db, status=IncidentStatus.closed, assign_reviewer=True)

    login_as(fx.admin)
    response = await client.post(
        fx.assign_url, json={"assigned_to": str(fx.other_reviewer.id)}
    )

    assert response.status_code == 409
    assert await count_notifications(db, fx.other_reviewer.id) == 0


# ---- The actor is never notified of their own action ----------------------------


async def test_actor_is_not_notified_of_own_transition(client, db):
    """The only path that reaches the self-notification guard, so it must be a real
    end-to-end test rather than a unit test of _deliver.

    `submitted_by` is a historical fact frozen at submission time, but a caller's role is
    resolved from the DB on every request. Promote the submitter to reviewer and they can
    transition their own earlier submission.
    """
    fx = await _setup(db)
    fx.staff.role = Role.reviewer
    await db.commit()

    login_as(fx.staff)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 200
    assert response.json()["status"] == "in_review"
    assert await count_notifications(db, fx.staff.id) == 0


# ---- UC-09 E1: the transition survives an undeliverable notification ------------


async def test_transition_survives_deactivated_recipient(client, db):
    """UC-09 E1. An inactive recipient cannot log in, so the row would be unreachable
    noise; the state change must still land."""
    fx = await _setup(db)
    fx.staff.status = UserStatus.inactive
    await db.commit()

    login_as(fx.reviewer)
    response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 200
    assert response.json()["status"] == "in_review"
    assert await count_notifications(db, fx.staff.id) == 0


async def test_deactivated_recipient_is_logged(client, db, caplog):
    """UC-09 E1's "System logs the error" half."""
    fx = await _setup(db)
    fx.staff.status = UserStatus.inactive
    await db.commit()

    login_as(fx.reviewer)
    with caplog.at_level(logging.WARNING, logger="flowdesk.audit"):
        await client.post(fx.url, json={"to_status": "in_review"})

    assert "notification_skipped" in caplog.text
    assert "reason=recipient_inactive" in caplog.text
    assert str(fx.staff.id) in caplog.text


async def test_deliver_skips_a_recipient_that_does_not_exist(db, caplog):
    """The other half of the pre-check, exercised directly.

    It cannot be driven through the API: `incidents.submitted_by` is a plain FK with no
    ON DELETE, so PostgreSQL refuses to remove a user who has submitted anything. That is
    also why the guard is cheap insurance rather than dead code — users are never hard
    deleted in the MVP (`user_service.set_status` only marks them inactive), but a tenant
    cascade or a future DELETE /users would make this routine, and the INSERT would then
    violate notifications.user_id -> users.id and take the caller's transaction with it.
    """
    fx = await _setup(db)
    ghost_id = uuid.uuid4()

    with caplog.at_level(logging.WARNING, logger="flowdesk.audit"):
        await notification_service._deliver(
            db,
            incident=fx.incident,
            actor=as_current_user(fx.reviewer),
            recipient_id=ghost_id,
            message="Should never be written.",
        )

    assert "reason=recipient_missing" in caplog.text
    assert await count_notifications(db, ghost_id) == 0
    await db.commit()  # the caller's session is untouched and still usable


async def test_transition_survives_a_failing_insert(client, db, caplog, monkeypatch):
    """The SAVEPOINT half of UC-09 E1: an insert that fails for any other reason is rolled
    back on its own, and BOTH the state change and its timeline row survive.

    `message` is NOT NULL, so returning None from _render reaches the failure at flush
    time inside the savepoint — the same shape as any real database-level rejection.
    """
    fx = await _setup(db)
    monkeypatch.setattr(notification_service, "_render", lambda *a, **kw: None)

    login_as(fx.reviewer)
    with caplog.at_level(logging.WARNING, logger="flowdesk.audit"):
        response = await client.post(fx.url, json={"to_status": "in_review"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "in_review"
    assert len(body["transitions"]) == 1  # the timeline row was NOT rolled back
    assert "reason=insert_failed" in caplog.text
    assert await count_notifications(db, fx.staff.id) == 0


# ---- Message rendering ----------------------------------------------------------


async def test_long_title_message_fits_the_column(client, db):
    """A maximum-length title (String(255)) still round-trips through String(500)."""
    fx = await _setup(db, title="P" * 255)

    login_as(fx.reviewer)
    assert (await client.post(fx.url, json={"to_status": "in_review"})).status_code == 200

    (notification,) = await _notifications_for(db, fx.staff)
    assert len(notification.message) <= notification_service.MESSAGE_MAX_LENGTH


def test_render_truncates_when_budget_exceeded(monkeypatch):
    """Unit test, no DB. Shrink the budget so the truncation branch actually executes."""
    monkeypatch.setattr(notification_service, "MESSAGE_MAX_LENGTH", 40)

    message = notification_service._render(
        notification_service._TRANSITIONED_TEMPLATE,
        title="X" * 200,
        state=IncidentStatus.in_review,
    )

    assert len(message) <= 40
    assert "…" in message
    assert "In Review" in message


def test_state_labels_cover_every_status():
    """Derived from the enum, so a new state cannot silently be missed."""
    assert set(notification_service._STATE_LABELS) == set(IncidentStatus)


@pytest.mark.parametrize(
    ("state", "label"),
    [
        (IncidentStatus.open, "Open"),
        (IncidentStatus.in_review, "In Review"),
        (IncidentStatus.closed, "Closed"),
    ],
)
def test_state_label_is_human_readable(state, label):
    assert notification_service._STATE_LABELS[state] == label
