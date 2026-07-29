"""Incident volume per week (UC-10 steps 2-3, 5; US-15).

The bucketing group is the point of this file. `created_at` is timestamptz and the host
clock is UTC, so a Monday-morning Melbourne incident is a Sunday in UTC and would be
misfiled a week early. That defect is silent — nothing crashes, the chart is just wrong —
so it is pinned here explicitly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Date, cast, func, literal, select

from app.models.enums import Role, Severity
from app.services import analytics_service
from tests.conftest import (
    login_as,
    seed_category,
    seed_incident,
    seed_tenant,
    seed_user,
)

URL = "/api/v1/analytics/volume"
MELBOURNE = ZoneInfo("Australia/Melbourne")
UTC = ZoneInfo("UTC")


class Fixture:
    def __init__(self, tenant, staff, reviewer, admin, sysadmin, category):
        self.tenant = tenant
        self.staff = staff
        self.reviewer = reviewer
        self.admin = admin
        self.sysadmin = sysadmin
        self.category = category


async def _setup(db, *, tenant_name: str = "Acme") -> Fixture:
    tenant = await seed_tenant(db, tenant_name)
    staff = await seed_user(db, tenant, Role.staff, email=f"staff@{tenant_name}.test")
    reviewer = await seed_user(db, tenant, Role.reviewer, email=f"rev@{tenant_name}.test")
    admin = await seed_user(
        db, tenant, Role.tenant_admin, email=f"admin@{tenant_name}.test"
    )
    sysadmin = await seed_user(
        db, tenant, Role.system_admin, email=f"sys@{tenant_name}.test"
    )
    category = await seed_category(db, tenant)
    return Fixture(tenant, staff, reviewer, admin, sysadmin, category)


async def _add(db, fx: Fixture, when: datetime, *, severity: Severity = Severity.medium):
    return await seed_incident(
        db,
        fx.tenant,
        category=fx.category,
        submitted_by=fx.staff,
        created_at=when,
        severity=severity,
    )


def _bucket(body: dict, day: date) -> int:
    (row,) = [b for b in body["data"] if b["week_start"] == day.isoformat()]
    return row["count"]


# ---- Bucketing: the timezone defect ---------------------------------------------


async def test_monday_9am_melbourne_lands_in_that_week(client, db):
    """THE regression test for this feature.

    2026-07-26T23:00Z is Monday 27 July 09:00 AEST. Bucketed in UTC it is a Sunday and
    falls into the week starting 20 July. If this test is deleted the feature is broken
    and nothing else will say so.
    """
    fx = await _setup(db)
    await _add(db, fx, datetime(2026, 7, 26, 23, 0, tzinfo=UTC))

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-13", "to": "2026-08-03"})).json()

    assert _bucket(body, date(2026, 7, 27)) == 1
    assert _bucket(body, date(2026, 7, 20)) == 0


async def test_sunday_late_melbourne_stays_in_the_previous_week(client, db):
    """The mirror case: Sunday 23:00 local is Monday in UTC, and must NOT jump forward."""
    fx = await _setup(db)
    await _add(db, fx, datetime(2026, 8, 2, 23, 0, tzinfo=MELBOURNE))

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-27", "to": "2026-08-10"})).json()

    assert _bucket(body, date(2026, 7, 27)) == 1
    assert _bucket(body, date(2026, 8, 3)) == 0


async def test_bucket_boundary_at_local_midnight_monday(client, db):
    """One second either side of local midnight must land in different weeks."""
    fx = await _setup(db)
    midnight = datetime(2026, 7, 27, 0, 0, tzinfo=MELBOURNE)
    await _add(db, fx, midnight - timedelta(seconds=1))
    await _add(db, fx, midnight)

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-20", "to": "2026-07-27"})).json()

    assert _bucket(body, date(2026, 7, 20)) == 1
    assert _bucket(body, date(2026, 7, 27)) == 1


async def test_dst_transition_week_buckets_correctly(client, db):
    """Melbourne starts daylight saving on 2026-10-04 (UTC+10 -> UTC+11). An incident on
    the Monday after must still bucket to that Monday."""
    fx = await _setup(db)
    await _add(db, fx, datetime(2026, 10, 5, 9, 0, tzinfo=MELBOURNE))

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-09-28", "to": "2026-10-12"})).json()

    assert _bucket(body, date(2026, 10, 5)) == 1


async def test_week_start_matches_postgres_date_trunc(db):
    """The pure-Python week_start() and PostgreSQL's ISO date_trunc('week', ...) must agree
    for every day, or the zero-fill would invent buckets the query never produced."""
    for offset in range(30):
        day = date(2026, 3, 25) + timedelta(days=offset)
        moment = datetime.combine(day, datetime.min.time(), tzinfo=MELBOURNE)
        from_db = await db.scalar(
            select(
                cast(
                    func.date_trunc(
                        "week",
                        func.timezone(literal("Australia/Melbourne"), literal(moment)),
                    ),
                    Date,
                )
            )
        )
        assert from_db == analytics_service.week_start(day), day


async def test_utc_reporting_timezone_shifts_buckets(client, db, monkeypatch):
    """Proves the setting reaches the SQL: under UTC the same incident moves a week back."""
    fx = await _setup(db)
    await _add(db, fx, datetime(2026, 7, 26, 23, 0, tzinfo=UTC))
    monkeypatch.setattr(analytics_service.settings, "reporting_timezone", "UTC")

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-13", "to": "2026-08-03"})).json()

    assert body["timezone"] == "UTC"
    assert _bucket(body, date(2026, 7, 20)) == 1
    assert _bucket(body, date(2026, 7, 27)) == 0


# ---- Zero-fill ------------------------------------------------------------------


async def test_empty_weeks_are_returned_as_zero(client, db):
    """A gap in a bar chart is ambiguous between "no incidents" and "no data collected"."""
    fx = await _setup(db)
    await _add(db, fx, datetime(2026, 7, 28, 10, 0, tzinfo=MELBOURNE))

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-13", "to": "2026-08-03"})).json()

    assert [b["count"] for b in body["data"]] == [0, 0, 1, 0]


async def test_bucket_count_matches_the_window(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-06", "to": "2026-07-27"})).json()

    assert len(body["data"]) == 4


async def test_buckets_are_ascending_and_unique(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-05-04", "to": "2026-07-27"})).json()

    starts = [b["week_start"] for b in body["data"]]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


async def test_no_incidents_returns_all_zero_buckets(client, db):
    """UC-10 E1: an empty organisation is a 200, not a 404."""
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.get(URL, params={"from": "2026-07-06", "to": "2026-07-27"})

    assert response.status_code == 200
    assert all(b["count"] == 0 for b in response.json()["data"])


# ---- Range handling -------------------------------------------------------------


async def test_default_window_is_twelve_weeks(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    body = (await client.get(URL)).json()

    assert len(body["data"]) == analytics_service.DEFAULT_WEEKS


async def test_response_echoes_the_effective_window(client, db):
    """`from`/`to` are snapped outward, so the axis can be labelled honestly."""
    fx = await _setup(db)

    login_as(fx.admin)
    body = (await client.get(URL, params={"from": "2026-07-30", "to": "2026-08-05"})).json()

    assert body["from"] == "2026-07-27"
    assert body["to"] == "2026-08-03"


async def test_response_echoes_the_timezone(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    assert (await client.get(URL)).json()["timezone"] == "Australia/Melbourne"


async def test_from_after_to_returns_422(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.get(URL, params={"from": "2026-08-01", "to": "2026-07-01"})

    assert response.status_code == 422
    assert response.json()["error"]["details"]["reason"] == "invalid_date_range"


async def test_range_over_53_weeks_returns_422(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.get(URL, params={"from": "1000-01-01", "to": "2026-07-27"})

    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert details["reason"] == "date_range_too_large"
    assert details["max_weeks"] == analytics_service.MAX_WEEKS


async def test_unparseable_date_returns_422(client, db):
    """FastAPI's native validation, so no extra reason slug is needed."""
    fx = await _setup(db)

    login_as(fx.admin)
    response = await client.get(URL, params={"from": "yesterday"})

    assert response.status_code == 422
    assert "errors" in response.json()["error"]["details"]


# ---- Severity filter (UC-10 step 5) ---------------------------------------------


async def test_filter_by_severity(client, db):
    fx = await _setup(db)
    when = datetime(2026, 7, 28, 10, 0, tzinfo=MELBOURNE)
    await _add(db, fx, when, severity=Severity.critical)
    await _add(db, fx, when, severity=Severity.low)

    login_as(fx.admin)
    body = (
        await client.get(
            URL, params={"from": "2026-07-27", "to": "2026-07-27", "severity": "critical"}
        )
    ).json()

    assert _bucket(body, date(2026, 7, 27)) == 1


async def test_severity_filter_is_echoed(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    body = (await client.get(URL, params={"severity": "high"})).json()

    assert body["severity"] == "high"


async def test_severity_is_null_when_not_filtered(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    assert (await client.get(URL)).json()["severity"] is None


async def test_unknown_severity_returns_422(client, db):
    fx = await _setup(db)

    login_as(fx.admin)
    assert (await client.get(URL, params={"severity": "apocalyptic"})).status_code == 422


# ---- Scope and RBAC -------------------------------------------------------------


async def test_tenant_admin_sees_only_own_tenant(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    when = datetime(2026, 7, 28, 10, 0, tzinfo=MELBOURNE)
    await _add(db, fx_a, when)
    await _add(db, fx_b, when)

    login_as(fx_a.admin)
    body = (await client.get(URL, params={"from": "2026-07-27", "to": "2026-07-27"})).json()

    assert _bucket(body, date(2026, 7, 27)) == 1


async def test_system_admin_is_cross_tenant(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    when = datetime(2026, 7, 28, 10, 0, tzinfo=MELBOURNE)
    await _add(db, fx_a, when)
    await _add(db, fx_b, when)

    login_as(fx_a.sysadmin)
    body = (await client.get(URL, params={"from": "2026-07-27", "to": "2026-07-27"})).json()

    assert _bucket(body, date(2026, 7, 27)) == 2


async def test_system_admin_tenant_id_pins_one_tenant(client, db):
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    when = datetime(2026, 7, 28, 10, 0, tzinfo=MELBOURNE)
    await _add(db, fx_a, when)
    await _add(db, fx_b, when)

    login_as(fx_a.sysadmin)
    body = (
        await client.get(
            URL,
            params={
                "from": "2026-07-27",
                "to": "2026-07-27",
                "tenant_id": str(fx_b.tenant.id),
            },
        )
    ).json()

    assert _bucket(body, date(2026, 7, 27)) == 1


async def test_tenant_id_is_ignored_for_tenant_admin(client, db):
    """Ignored, not rejected — matching list_incidents and list_users."""
    fx_a = await _setup(db, tenant_name="Acme")
    fx_b = await _setup(db, tenant_name="Globex")
    when = datetime(2026, 7, 28, 10, 0, tzinfo=MELBOURNE)
    await _add(db, fx_a, when)
    await _add(db, fx_b, when)
    await _add(db, fx_b, when)

    login_as(fx_a.admin)
    body = (
        await client.get(
            URL,
            params={
                "from": "2026-07-27",
                "to": "2026-07-27",
                "tenant_id": str(fx_b.tenant.id),
            },
        )
    ).json()

    assert _bucket(body, date(2026, 7, 27)) == 1  # still Acme's own count


@pytest.mark.parametrize("role", [Role.staff, Role.reviewer])
async def test_non_admin_roles_are_403(client, db, role):
    """Not merely an unmodelled role: analytics count incidents these callers may not
    read, so answering would leak their existence (NFR-12)."""
    fx = await _setup(db)
    user = await seed_user(db, fx.tenant, role, email=f"{role.value}@vol.test")

    login_as(user)
    response = await client.get(URL)

    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "insufficient_role"


async def test_unauthenticated_401(client, db):
    await _setup(db)

    assert (await client.get(URL)).status_code == 401


async def test_incident_created_at_defaults_are_bucketed_too(client, db):
    """Nothing special about seeded timestamps: an incident created through the API in the
    normal way appears in the current week."""
    fx = await _setup(db)

    login_as(fx.staff)
    created = await client.post(
        "/api/v1/incidents",
        json={
            "title": "Printer on fire",
            "description": "Third-floor printer.",
            "category_id": str(fx.category.id),
            "severity": "high",
        },
    )
    assert created.status_code == 201

    login_as(fx.admin)
    body = (await client.get(URL)).json()

    assert body["data"][-1]["week_start"] == body["to"]
    assert sum(b["count"] for b in body["data"]) == 1
