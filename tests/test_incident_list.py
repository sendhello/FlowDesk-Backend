"""Role-scoped incident listing (UC-07; US-09)."""

from __future__ import annotations

import pytest

from app.models.enums import IncidentStatus, Role, Severity
from app.models.incident import Incident
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)


async def _ids(client, query: str = "") -> list[str]:
    response = await client.get(f"/api/v1/incidents{query}")
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["data"]]


async def test_staff_list_shows_only_own_submissions(client, db):
    tenant = await seed_tenant(db, "Acme")
    author = await seed_user(db, tenant, Role.staff, email="author@acme.com")
    other = await seed_user(db, tenant, Role.staff, email="other@acme.com")
    category = await seed_category(db, tenant)
    mine = await seed_incident(db, tenant, category=category, submitted_by=author)
    theirs = await seed_incident(db, tenant, category=category, submitted_by=other)

    login_as(author)
    ids = await _ids(client)

    assert ids == [str(mine.id)]
    assert str(theirs.id) not in ids


async def test_reviewer_list_shows_assigned_and_unassigned(client, db):
    """The unassigned arm is what makes the workflow reachable at all: UC-06 creates
    incidents with no assignee, so a strict `assigned_to == me` scope would hide every new
    incident from every reviewer."""
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    reviewer = await seed_user(db, tenant, Role.reviewer, email="rev@acme.com")
    category = await seed_category(db, tenant)
    unassigned = await seed_incident(db, tenant, category=category, submitted_by=staff)
    assigned = await seed_incident(
        db, tenant, category=category, submitted_by=staff, assigned_to=reviewer
    )

    login_as(reviewer)
    ids = await _ids(client)

    assert set(ids) == {str(unassigned.id), str(assigned.id)}


async def test_reviewer_list_excludes_incidents_assigned_to_others(client, db):
    tenant = await seed_tenant(db, "Acme")
    staff = await seed_user(db, tenant, Role.staff)
    mine = await seed_user(db, tenant, Role.reviewer, email="mine@acme.com")
    theirs = await seed_user(db, tenant, Role.reviewer, email="theirs@acme.com")
    category = await seed_category(db, tenant)
    other_work = await seed_incident(
        db, tenant, category=category, submitted_by=staff, assigned_to=theirs
    )

    login_as(mine)
    ids = await _ids(client)

    assert str(other_work.id) not in ids


async def test_tenant_admin_list_shows_all_in_tenant(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    reviewer = await seed_user(db, tenant, Role.reviewer)
    category = await seed_category(db, tenant)
    first = await seed_incident(db, tenant, category=category, submitted_by=staff)
    second = await seed_incident(
        db, tenant, category=category, submitted_by=staff, assigned_to=reviewer
    )

    login_as(admin)
    ids = await _ids(client)

    assert set(ids) == {str(first.id), str(second.id)}


async def test_system_admin_list_is_cross_tenant(client, db):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    root = await seed_user(db, tenant_a, Role.system_admin, email="root@flowdesk.io")
    staff_a = await seed_user(db, tenant_a, Role.staff, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="b@globex.com")
    incident_a = await seed_incident(
        db, tenant_a, category=await seed_category(db, tenant_a), submitted_by=staff_a
    )
    incident_b = await seed_incident(
        db, tenant_b, category=await seed_category(db, tenant_b), submitted_by=staff_b
    )

    login_as(root)
    ids = await _ids(client)

    assert set(ids) == {str(incident_a.id), str(incident_b.id)}


async def test_system_admin_list_filtered_by_tenant_id_query(client, db):
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    root = await seed_user(db, tenant_a, Role.system_admin, email="root@flowdesk.io")
    staff_a = await seed_user(db, tenant_a, Role.staff, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="b@globex.com")
    incident_a = await seed_incident(
        db, tenant_a, category=await seed_category(db, tenant_a), submitted_by=staff_a
    )
    await seed_incident(
        db, tenant_b, category=await seed_category(db, tenant_b), submitted_by=staff_b
    )

    login_as(root)
    ids = await _ids(client, f"?tenant_id={tenant_b.id}")

    assert str(incident_a.id) not in ids
    assert len(ids) == 1


async def test_tenant_admin_list_excludes_other_tenants(client, db):
    """NFR-12."""
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    admin_a = await seed_user(db, tenant_a, Role.tenant_admin, email="a@acme.com")
    staff_b = await seed_user(db, tenant_b, Role.staff, email="b@globex.com")
    await seed_incident(
        db, tenant_b, category=await seed_category(db, tenant_b), submitted_by=staff_b
    )

    login_as(admin_a)
    response = await client.get("/api/v1/incidents")

    assert response.json()["pagination"]["total"] == 0


async def test_filter_by_status(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    await seed_incident(db, tenant, category=category, submitted_by=staff)
    closed = await seed_incident(
        db, tenant, category=category, submitted_by=staff, status=IncidentStatus.closed
    )

    login_as(admin)
    ids = await _ids(client, "?status=closed")

    assert ids == [str(closed.id)]


async def test_filter_by_severity(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    await seed_incident(db, tenant, category=category, submitted_by=staff)
    critical = await seed_incident(
        db, tenant, category=category, submitted_by=staff, severity=Severity.critical
    )

    login_as(admin)
    ids = await _ids(client, "?severity=critical")

    assert ids == [str(critical.id)]


async def test_filter_by_status_and_severity_combined(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    await seed_incident(
        db, tenant, category=category, submitted_by=staff, severity=Severity.critical
    )
    await seed_incident(
        db, tenant, category=category, submitted_by=staff, status=IncidentStatus.closed
    )
    both = await seed_incident(
        db,
        tenant,
        category=category,
        submitted_by=staff,
        severity=Severity.critical,
        status=IncidentStatus.closed,
    )

    login_as(admin)
    ids = await _ids(client, "?status=closed&severity=critical")

    assert ids == [str(both.id)]


async def test_invalid_status_filter_returns_422(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.get("/api/v1/incidents?status=archived")

    assert response.status_code == 422


async def test_default_sort_is_created_at_desc(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    first = await seed_incident(db, tenant, category=category, submitted_by=staff)
    second = await seed_incident(db, tenant, category=category, submitted_by=staff)

    login_as(admin)
    ids = await _ids(client)

    assert ids == [str(second.id), str(first.id)]


async def test_sort_by_title_asc(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    zulu = await seed_incident(
        db, tenant, category=category, submitted_by=staff, title="Zulu"
    )
    alpha = await seed_incident(
        db, tenant, category=category, submitted_by=staff, title="Alpha"
    )

    login_as(admin)
    ids = await _ids(client, "?sort=title&order=asc")

    assert ids == [str(alpha.id), str(zulu.id)]


async def test_sort_by_severity_ranks_low_to_critical(client, db):
    """The native PostgreSQL enum's declaration order is already the semantic order."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    for severity in (Severity.critical, Severity.low, Severity.high, Severity.medium):
        await seed_incident(
            db, tenant, category=category, submitted_by=staff, severity=severity
        )

    login_as(admin)
    response = await client.get("/api/v1/incidents?sort=severity&order=asc")

    assert [row["severity"] for row in response.json()["data"]] == [
        "low",
        "medium",
        "high",
        "critical",
    ]


async def test_sort_by_status_ranks_open_to_closed(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    for state in (IncidentStatus.closed, IncidentStatus.open, IncidentStatus.in_review):
        await seed_incident(
            db, tenant, category=category, submitted_by=staff, status=state
        )

    login_as(admin)
    response = await client.get("/api/v1/incidents?sort=status&order=asc")

    assert [row["status"] for row in response.json()["data"]] == [
        "open",
        "in_review",
        "closed",
    ]


async def test_invalid_sort_field_returns_422(client, db):
    """`sort` is a closed enum, so no caller-supplied text reaches an ORDER BY clause."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)
    response = await client.get("/api/v1/incidents?sort=submitted_by")

    assert response.status_code == 422


async def test_pagination_limit_and_offset(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    for index in range(5):
        await seed_incident(
            db, tenant, category=category, submitted_by=staff, title=f"Incident {index}"
        )

    login_as(admin)
    response = await client.get("/api/v1/incidents?limit=2&offset=2&sort=title&order=asc")

    body = response.json()
    assert body["pagination"] == {"limit": 2, "offset": 2, "total": 5}
    assert [row["title"] for row in body["data"]] == ["Incident 2", "Incident 3"]


async def test_pagination_limit_zero_returns_422(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)

    assert (await client.get("/api/v1/incidents?limit=0")).status_code == 422


async def test_pagination_limit_above_maximum_returns_422(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)

    login_as(admin)

    assert (await client.get("/api/v1/incidents?limit=101")).status_code == 422


async def test_pagination_offset_beyond_total_returns_empty_page(client, db):
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    await seed_incident(db, tenant, category=category, submitted_by=staff)

    login_as(admin)
    body = (await client.get("/api/v1/incidents?offset=50")).json()

    assert body["data"] == []
    assert body["pagination"]["total"] == 1


async def test_pagination_is_stable_when_sort_key_ties(client, db):
    """Three rows written in ONE transaction share a created_at (PostgreSQL now() is
    transaction start time). Without the id tiebreak, paging one at a time could repeat or
    skip rows."""
    tenant = await seed_tenant(db, "Acme")
    admin = await seed_user(db, tenant, Role.tenant_admin)
    staff = await seed_user(db, tenant, Role.staff)
    category = await seed_category(db, tenant)
    for index in range(3):
        db.add(
            Incident(
                tenant_id=tenant.id,
                category_id=category.id,
                submitted_by=staff.id,
                title=f"Tied {index}",
                description="Same transaction, same timestamp.",
                severity=Severity.medium,
                status=IncidentStatus.open,
            )
        )
    await db.commit()

    login_as(admin)
    paged: list[str] = []
    for offset in range(3):
        paged.extend(await _ids(client, f"?limit=1&offset={offset}"))

    assert len(set(paged)) == 3, "pagination repeated or skipped a row on a tied sort key"


@pytest.mark.parametrize(
    "role", [Role.staff, Role.reviewer, Role.tenant_admin, Role.system_admin]
)
async def test_incident_absent_from_list_is_404_on_detail(client, db, role):
    """The single-predicate invariant: a detail 404 exactly matches a list omission, for
    every role. Both paths share incident_service._visibility_conds."""
    tenant_a = await seed_tenant(db, "Acme")
    tenant_b = await seed_tenant(db, "Globex")
    caller = await seed_user(db, tenant_a, role, email="caller@acme.com")
    author_a = await seed_user(db, tenant_a, Role.staff, email="author@acme.com")
    author_b = await seed_user(db, tenant_b, Role.staff, email="author@globex.com")
    candidates = [
        await seed_incident(
            db,
            tenant_a,
            category=await seed_category(db, tenant_a),
            submitted_by=author_a,
        ),
        await seed_incident(
            db,
            tenant_b,
            category=await seed_category(db, tenant_b),
            submitted_by=author_b,
        ),
    ]

    login_as(caller)
    visible = set(await _ids(client, "?limit=100"))

    for incident in candidates:
        expected = 200 if str(incident.id) in visible else 404
        response = await client.get(f"/api/v1/incidents/{incident.id}")
        assert response.status_code == expected, (
            f"{role.value}: incident {incident.id} listed={str(incident.id) in visible} "
            f"but detail returned {response.status_code}"
        )
