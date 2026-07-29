"""Notification panel: list, unread badge, mark-read, dismiss-all (UC-09; US-14)."""

from __future__ import annotations

import uuid

import pytest

from app.models.enums import Role
from app.models.notification import Notification
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_notification,
    seed_tenant,
    seed_user,
)

LIST_URL = "/api/v1/notifications"
COUNT_URL = "/api/v1/notifications/unread-count"
READ_ALL_URL = "/api/v1/notifications/read-all"


class Fixture:
    def __init__(self, tenant, staff, other_staff, reviewer, admin, incident):
        self.tenant = tenant
        self.staff = staff
        self.other_staff = other_staff
        self.reviewer = reviewer
        self.admin = admin
        self.incident = incident


async def _setup(db, *, tenant_name: str = "Acme") -> Fixture:
    tenant = await seed_tenant(db, tenant_name)
    staff = await seed_user(db, tenant, Role.staff, email=f"staff@{tenant_name}.test")
    other_staff = await seed_user(
        db, tenant, Role.staff, email=f"staff2@{tenant_name}.test"
    )
    reviewer = await seed_user(db, tenant, Role.reviewer, email=f"rev@{tenant_name}.test")
    admin = await seed_user(
        db, tenant, Role.tenant_admin, email=f"admin@{tenant_name}.test"
    )
    category = await seed_category(db, tenant)
    incident = await seed_incident(
        db, tenant, category=category, submitted_by=staff
    )
    return Fixture(tenant, staff, other_staff, reviewer, admin, incident)


def _read_url(notification_id) -> str:
    return f"/api/v1/notifications/{notification_id}/read"


# ---- List (UC-09 step 5) --------------------------------------------------------


async def test_list_returns_page_envelope(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    response = await client.get(LIST_URL)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "pagination"}
    assert body["pagination"] == {"limit": 50, "offset": 0, "total": 1}


async def test_list_is_newest_first(client, db):
    fx = await _setup(db)
    for i in range(3):
        await seed_notification(
            db, fx.tenant, user=fx.staff, incident=fx.incident, message=f"n{i}"
        )

    login_as(fx.staff)
    data = (await client.get(LIST_URL)).json()["data"]

    assert [row["message"] for row in data] == ["n2", "n1", "n0"]


async def test_list_is_stable_when_created_at_ties(client, db):
    """Three rows inserted in ONE transaction share created_at (now() is transaction start
    time in PostgreSQL). Paging one at a time must not duplicate or skip — that is the
    `id DESC` tiebreak doing its job.
    """
    fx = await _setup(db)
    for i in range(3):
        db.add(
            Notification(
                tenant_id=fx.tenant.id,
                user_id=fx.staff.id,
                incident_id=fx.incident.id,
                message=f"tie-{i}",
            )
        )
    await db.commit()

    login_as(fx.staff)
    seen = []
    for offset in range(3):
        page = (await client.get(LIST_URL, params={"limit": 1, "offset": offset})).json()
        seen.append(page["data"][0]["id"])

    assert len(set(seen)) == 3


async def test_list_excludes_other_users_notifications(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.other_staff, incident=fx.incident)

    login_as(fx.staff)
    body = (await client.get(LIST_URL)).json()

    assert body["data"] == []
    assert body["pagination"]["total"] == 0


async def test_list_excludes_other_tenants_notifications(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    await seed_notification(db, fx_b.tenant, user=fx_b.staff, incident=fx_b.incident)

    login_as(fx_a.staff)
    assert (await client.get(LIST_URL)).json()["pagination"]["total"] == 0


async def test_list_is_empty_for_a_new_user(client, db):
    fx = await _setup(db)

    login_as(fx.reviewer)
    body = (await client.get(LIST_URL)).json()

    assert body["data"] == []
    assert body["pagination"]["total"] == 0


async def test_filter_is_read_false(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident, message="u")
    await seed_notification(
        db, fx.tenant, user=fx.staff, incident=fx.incident, message="r", is_read=True
    )

    login_as(fx.staff)
    body = (await client.get(LIST_URL, params={"is_read": "false"})).json()

    assert [row["message"] for row in body["data"]] == ["u"]
    assert body["pagination"]["total"] == 1


async def test_filter_is_read_true(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident, message="u")
    await seed_notification(
        db, fx.tenant, user=fx.staff, incident=fx.incident, message="r", is_read=True
    )

    login_as(fx.staff)
    body = (await client.get(LIST_URL, params={"is_read": "true"})).json()

    assert [row["message"] for row in body["data"]] == ["r"]


async def test_pagination_limit_and_offset(client, db):
    fx = await _setup(db)
    for i in range(5):
        await seed_notification(
            db, fx.tenant, user=fx.staff, incident=fx.incident, message=f"n{i}"
        )

    login_as(fx.staff)
    body = (await client.get(LIST_URL, params={"limit": 2, "offset": 2})).json()

    assert len(body["data"]) == 2
    assert body["pagination"]["total"] == 5


async def test_list_row_shape(client, db):
    """NFR-05: no user_id, no tenant_id — the caller IS the user."""
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    (row,) = (await client.get(LIST_URL)).json()["data"]

    assert set(row) == {"id", "incident_id", "message", "is_read", "created_at"}
    assert row["incident_id"] == str(fx.incident.id)


# ---- Unread count (UC-09 step 3) ------------------------------------------------


async def test_unread_count_zero_initially(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    response = await client.get(COUNT_URL)

    assert response.status_code == 200
    assert response.json() == {"unread": 0}


async def test_unread_count_after_transitions(client, db):
    """The badge counts what dispatch actually wrote, end to end."""
    fx = await _setup(db)

    login_as(fx.reviewer)
    await client.post(
        f"/api/v1/incidents/{fx.incident.id}/transitions", json={"to_status": "in_review"}
    )

    login_as(fx.staff)
    assert (await client.get(COUNT_URL)).json() == {"unread": 1}


async def test_unread_count_excludes_read(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)
    await seed_notification(
        db, fx.tenant, user=fx.staff, incident=fx.incident, is_read=True
    )

    login_as(fx.staff)
    assert (await client.get(COUNT_URL)).json() == {"unread": 1}


async def test_unread_count_is_per_user(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.other_staff, incident=fx.incident)

    login_as(fx.staff)
    assert (await client.get(COUNT_URL)).json() == {"unread": 0}


async def test_unread_count_matches_filtered_list_total(client, db):
    """The badge and `?is_read=false` are two ways to the same number. They are built from
    one predicate (`_recipient_conds`), and this pins that they agree."""
    fx = await _setup(db)
    for _ in range(3):
        await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)
    await seed_notification(
        db, fx.tenant, user=fx.staff, incident=fx.incident, is_read=True
    )

    login_as(fx.staff)
    badge = (await client.get(COUNT_URL)).json()["unread"]
    listed = (await client.get(LIST_URL, params={"is_read": "false"})).json()

    assert badge == listed["pagination"]["total"] == 3


# ---- Mark one read (UC-09 steps 6-7) --------------------------------------------


async def test_mark_read_flips_the_flag(client, db):
    fx = await _setup(db)
    n = await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    response = await client.post(_read_url(n.id))

    assert response.status_code == 200
    assert response.json()["is_read"] is True


async def test_mark_read_returns_the_row(client, db):
    fx = await _setup(db)
    n = await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    body = (await client.post(_read_url(n.id))).json()

    assert body["id"] == str(n.id)
    assert body["incident_id"] == str(fx.incident.id)


async def test_mark_read_decrements_unread_count(client, db):
    fx = await _setup(db)
    n = await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    await client.post(_read_url(n.id))

    assert (await client.get(COUNT_URL)).json() == {"unread": 1}


async def test_mark_read_is_idempotent(client, db):
    """Idempotent by postcondition, the same reasoning as NFR-06: the second call finds
    the row already read and writes nothing."""
    fx = await _setup(db)
    n = await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    first = await client.post(_read_url(n.id))
    second = await client.post(_read_url(n.id))

    assert second.status_code == 200
    assert second.json() == first.json()


async def test_mark_read_unknown_id_404(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    response = await client.post(_read_url(uuid.uuid4()))

    assert response.status_code == 404


async def test_mark_read_other_users_notification_404(client, db):
    fx = await _setup(db)
    n = await seed_notification(db, fx.tenant, user=fx.other_staff, incident=fx.incident)

    login_as(fx.staff)
    assert (await client.post(_read_url(n.id))).status_code == 404


async def test_mark_read_other_tenants_notification_404(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    n = await seed_notification(db, fx_b.tenant, user=fx_b.staff, incident=fx_b.incident)

    login_as(fx_a.staff)
    assert (await client.post(_read_url(n.id))).status_code == 404


async def test_mark_read_404_bodies_are_identical(client, db):
    """NFR-12: "does not exist", "someone else's" and "another tenant's" must be
    indistinguishable."""
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    theirs = await seed_notification(
        db, fx_a.tenant, user=fx_a.other_staff, incident=fx_a.incident
    )
    cross = await seed_notification(
        db, fx_b.tenant, user=fx_b.staff, incident=fx_b.incident
    )

    login_as(fx_a.staff)
    bodies = [
        (await client.post(_read_url(uuid.uuid4()))).json(),
        (await client.post(_read_url(theirs.id))).json(),
        (await client.post(_read_url(cross.id))).json(),
    ]

    assert bodies[0] == bodies[1] == bodies[2]


async def test_mark_read_reason_slug(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    body = (await client.post(_read_url(uuid.uuid4()))).json()

    assert body["error"]["details"]["reason"] == "notification_not_found"


async def test_mark_read_does_not_flip_another_users_row(client, db):
    fx = await _setup(db)
    n = await seed_notification(db, fx.tenant, user=fx.other_staff, incident=fx.incident)

    login_as(fx.staff)
    await client.post(_read_url(n.id))

    login_as(fx.other_staff)
    assert (await client.get(COUNT_URL)).json() == {"unread": 1}


async def test_mark_read_malformed_uuid_422(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    assert (await client.post("/api/v1/notifications/not-a-uuid/read")).status_code == 422


# ---- Dismiss all (UC-09 A1) -----------------------------------------------------


async def test_read_all_marks_everything(client, db):
    fx = await _setup(db)
    for _ in range(3):
        await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    assert (await client.post(READ_ALL_URL)).status_code == 200

    body = (await client.get(LIST_URL, params={"is_read": "false"})).json()
    assert body["pagination"]["total"] == 0


async def test_read_all_resets_unread_to_zero(client, db):
    """UC-09 A1.2, asserted from the response body — no second round-trip needed."""
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)

    login_as(fx.staff)
    assert (await client.post(READ_ALL_URL)).json()["unread"] == 0


async def test_read_all_returns_marked_count(client, db):
    fx = await _setup(db)
    for _ in range(3):
        await seed_notification(db, fx.tenant, user=fx.staff, incident=fx.incident)
    await seed_notification(
        db, fx.tenant, user=fx.staff, incident=fx.incident, is_read=True
    )

    login_as(fx.staff)
    assert (await client.post(READ_ALL_URL)).json()["marked_read"] == 3


async def test_read_all_with_nothing_unread_returns_zero(client, db):
    fx = await _setup(db)

    login_as(fx.staff)
    assert (await client.post(READ_ALL_URL)).json() == {"marked_read": 0, "unread": 0}


async def test_read_all_does_not_touch_other_users(client, db):
    fx = await _setup(db)
    await seed_notification(db, fx.tenant, user=fx.other_staff, incident=fx.incident)

    login_as(fx.staff)
    assert (await client.post(READ_ALL_URL)).json()["marked_read"] == 0

    login_as(fx.other_staff)
    assert (await client.get(COUNT_URL)).json() == {"unread": 1}


# ---- RBAC -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "role", [Role.staff, Role.reviewer, Role.tenant_admin, Role.system_admin]
)
async def test_every_role_can_read_own_notifications(client, db, role):
    """Notifications are not role-gated: every role receives them."""
    fx = await _setup(db)
    user = await seed_user(db, fx.tenant, role, email=f"{role.value}@panel.test")

    login_as(user)
    assert (await client.get(LIST_URL)).status_code == 200
    assert (await client.get(COUNT_URL)).status_code == 200


async def test_unauthenticated_401(client, db):
    await _setup(db)

    assert (await client.get(LIST_URL)).status_code == 401
    assert (await client.get(COUNT_URL)).status_code == 401
