"""Status distribution (UC-10 step 4; US-16)."""

from __future__ import annotations

import pytest

from app.models.enums import IncidentStatus, Role
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)

URL = "/api/v1/analytics/status-distribution"


class Fixture:
    def __init__(self, tenant, staff, admin, sysadmin, category):
        self.tenant = tenant
        self.staff = staff
        self.admin = admin
        self.sysadmin = sysadmin
        self.category = category


async def _setup(db, *, tenant_name: str = "Acme") -> Fixture:
    tenant = await seed_tenant(db, tenant_name)
    staff = await seed_user(db, tenant, Role.staff, email=f"staff@{tenant_name}.test")
    admin = await seed_user(
        db, tenant, Role.tenant_admin, email=f"admin@{tenant_name}.test"
    )
    sysadmin = await seed_user(
        db, tenant, Role.system_admin, email=f"sys@{tenant_name}.test"
    )
    category = await seed_category(db, tenant)
    return Fixture(tenant, staff, admin, sysadmin, category)


async def _add(db, fx: Fixture, status: IncidentStatus):
    return await seed_incident(
        db, fx.tenant, category=fx.category, submitted_by=fx.staff, status=status
    )


def _counts(body: dict) -> dict[str, int]:
    return {row["status"]: row["count"] for row in body["data"]}


async def test_counts_by_status(client, db):
    fx = await _setup(db)
    await _add(db, fx, IncidentStatus.open)
    await _add(db, fx, IncidentStatus.open)
    await _add(db, fx, IncidentStatus.in_review)
    await _add(db, fx, IncidentStatus.closed)

    login_as(fx.admin)
    body = (await client.get(URL)).json()

    assert _counts(body) == {"open": 2, "in_review": 1, "closed": 1}


async def test_all_three_statuses_present_when_empty(client, db):
    """UC-10 E1: no data is a 200 with zeros, never a 404. The frontend never branches on
    a missing key."""
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.get(URL)

    assert response.status_code == 200
    assert _counts(response.json()) == {"open": 0, "in_review": 0, "closed": 0}


async def test_status_order_is_enum_declaration_order(client, db):
    """open -> in_review -> closed is the workflow order, so the chart reads left to right
    the way the process runs."""
    fx = await _setup(db)
    await _add(db, fx, IncidentStatus.closed)

    login_as(fx.admin)
    body = (await client.get(URL)).json()

    assert [row["status"] for row in body["data"]] == ["open", "in_review", "closed"]


async def test_total_equals_sum_of_counts(client, db):
    fx = await _setup(db)
    for status in (IncidentStatus.open, IncidentStatus.in_review, IncidentStatus.closed):
        await _add(db, fx, status)

    login_as(fx.admin)
    body = (await client.get(URL)).json()

    assert body["total"] == sum(row["count"] for row in body["data"]) == 3


async def test_total_zero_when_no_incidents(client, db):
    """`total == 0` is the single field the frontend checks to render UC-10 E1's empty
    state."""
    fx = await _setup(db)

    login_as(fx.admin)
    assert (await client.get(URL)).json()["total"] == 0


async def test_excludes_other_tenants(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    await _add(db, fx_a, IncidentStatus.open)
    await _add(db, fx_b, IncidentStatus.open)
    await _add(db, fx_b, IncidentStatus.closed)

    login_as(fx_a.admin)
    body = (await client.get(URL)).json()

    assert body["total"] == 1


async def test_system_admin_is_cross_tenant(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    await _add(db, fx_a, IncidentStatus.open)
    await _add(db, fx_b, IncidentStatus.closed)

    login_as(fx_a.sysadmin)
    body = (await client.get(URL)).json()

    assert _counts(body) == {"open": 1, "in_review": 0, "closed": 1}


async def test_system_admin_tenant_id_filter(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    await _add(db, fx_a, IncidentStatus.open)
    await _add(db, fx_b, IncidentStatus.closed)

    login_as(fx_a.sysadmin)
    body = (await client.get(URL, params={"tenant_id": str(fx_b.tenant.id)})).json()

    assert _counts(body) == {"open": 0, "in_review": 0, "closed": 1}


async def test_counts_reflect_workflow_transitions(client, db):
    """The distribution is a live snapshot, so a transition moves an incident between
    buckets immediately."""
    fx = await _setup(db)
    reviewer = await seed_user(db, fx.tenant, Role.reviewer, email="rev@acme.test")
    incident = await _add(db, fx, IncidentStatus.open)

    login_as(reviewer)
    await client.post(
        f"/api/v1/incidents/{incident.id}/transitions", json={"to_status": "in_review"}
    )

    login_as(fx.admin)
    assert _counts((await client.get(URL)).json()) == {
        "open": 0,
        "in_review": 1,
        "closed": 0,
    }


@pytest.mark.parametrize("role", [Role.staff, Role.reviewer])
async def test_non_admin_roles_are_403(client, db, role):
    """These endpoints count every incident in the tenant — more than a staff or reviewer
    caller may read (NFR-12)."""
    fx = await _setup(db)
    user = await seed_user(db, fx.tenant, role, email=f"{role.value}@dist.test")

    login_as(user)
    response = await client.get(URL)

    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "insufficient_role"


async def test_unauthenticated_401(client, db):
    await _setup(db)

    assert (await client.get(URL)).status_code == 401
