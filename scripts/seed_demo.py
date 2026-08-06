"""Seed the deployed FlowDesk instance with demonstration data — through the public API.

    uv run python -m scripts.seed_demo --all --dry-run
    uv run python -m scripts.seed_demo --all --yes

Every business fact is created by calling the same endpoints the frontend calls, so the
resulting data is evidence that the API works: the UC-08 state machine, the US-13
notification hooks, RBAC and tenant scoping all execute normally. Nothing is inserted
behind the application's back. Exactly two things cannot go through the API, and both live
in `scripts/seed_db.py`: deleting a previous run, and rewriting timestamps (see that
module's docstring).

Why users are provisioned the way they are
------------------------------------------
`users.id` IS the Supabase Auth id (app/models/user.py), so no row exists without an auth
user, and the only endpoints that create one — `POST /organizations` and `POST /users` —
both call GoTrue *invite*, which sends an email. Supabase's built-in SMTP allows only a
couple of messages per hour, so inviting ten users would fail halfway.

`user_service.invite_user` already has the way out. When GoTrue reports the address is
taken it calls `get_user_by_email` and provisions the row against the existing auth id — a
documented idempotent-recovery path. So each staff/reviewer is created first through the
Auth Admin API (`email_confirm: true`, no email at all) and then through
`POST /api/v1/users`, which takes that branch and writes the `users` row.

`tenant_service.register_organization` has no such branch — an existing address is a hard
409 — so the first admin of each organisation must be a genuine invite. That is one email
per organisation, two per run, and it is irreducible without changing the product code.

Since the backend has no login endpoint, tokens come from Supabase directly
(`POST /auth/v1/token?grant_type=password`), which is what the frontend does. Seeded users
therefore need a password: `SEED_DEMO_PASSWORD`, set through the Auth Admin API, never
stored in this repository.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.models.enums import IncidentStatus, Role
from scripts import seed_db, seed_fixtures
from scripts.seed_fixtures import IncidentPlan, OrgPlan, OrgSpec, UserSpec

DEFAULT_API_BASE = "https://flowdesk-backend.fly.dev"
DEFAULT_RANDOM_SEED = 20260803
DEFAULT_CONCURRENCY = 4

#: `get_user_by_email` (app/services/supabase_admin.py) reads ONE page of GoTrue's admin
#: user list — 50 rows — and does not paginate. Past that, the recovery branch this seeder
#: depends on silently returns None and every invite becomes a 409. Refuse to start.
AUTH_USER_SOFT_LIMIT = 45

#: Share of each user's notifications left unread, so the UC-09 bell badge is non-zero.
UNREAD_SHARE = 0.4


class SeedError(RuntimeError):
    """Aborts the run with a message the operator can act on."""


# ---- Output -----------------------------------------------------------------------


def info(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    print(f"  · {message}", flush=True)


def warn(message: str) -> None:
    print(f"  ! {message}", flush=True)


def heading(message: str) -> None:
    print(f"\n=== {message} ===", flush=True)


# ---- HTTP -------------------------------------------------------------------------


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 5,
    backoff: tuple[float, ...] = (1.0, 3.0, 8.0, 20.0),
    **kwargs: Any,
) -> httpx.Response:
    """Issue a request, retrying only what is safe to retry.

    429 and 5xx are transient (the Fly machine is a single shared-cpu-1x, and GoTrue rate
    limits email); 4xx below 500 is a decision by the server and must surface immediately
    so the caller can act on the error envelope.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
        else:
            if response.status_code < 500 and response.status_code != 429:
                return response
            last_exc = None
            if attempt == attempts - 1:
                return response
        await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])
    raise SeedError(f"{method} {url} failed after {attempts} attempts: {last_exc}")


def _api_error(response: httpx.Response) -> str:
    """Render FlowDesk's error envelope `{"error": {code, message, details}}`."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        details = error.get("details") or {}
        suffix = f" {details}" if details else ""
        return f"HTTP {response.status_code} {error.get('code')}: {error.get('message')}{suffix}"
    return f"HTTP {response.status_code}: {response.text[:300]}"


class SupabaseAuth:
    """Auth Admin API + the password grant. The only user of the service-role key here."""

    def __init__(self, client: httpx.AsyncClient, *, base: str, service_key: str, anon_key: str):
        self._client = client
        self._base = base.rstrip("/")
        self._service_key = service_key
        self._anon_key = anon_key

    def _admin_headers(self) -> dict[str, str]:
        return {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Content-Type": "application/json",
        }

    async def list_users(self) -> list[dict]:
        """Every auth user, paginated properly (unlike the app's best-effort lookup)."""
        users: list[dict] = []
        page = 1
        while True:
            response = await _request_with_retry(
                self._client,
                "GET",
                f"{self._base}/auth/v1/admin/users",
                params={"page": page, "per_page": 200},
                headers=self._admin_headers(),
            )
            if response.status_code >= 400:
                raise SeedError(f"Auth list_users failed: {_api_error(response)}")
            batch = response.json().get("users", [])
            users.extend(batch)
            if len(batch) < 200:
                return users
            page += 1

    async def create_user(self, *, email: str, password: str, name: str) -> uuid.UUID:
        """Create a confirmed user with a password. Sends NO email."""
        response = await _request_with_retry(
            self._client,
            "POST",
            f"{self._base}/auth/v1/admin/users",
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {"name": name},
            },
            headers=self._admin_headers(),
        )
        if response.status_code >= 400:
            raise SeedError(f"Auth create_user({email}) failed: {_api_error(response)}")
        return uuid.UUID(response.json()["id"])

    async def set_password(self, user_id: uuid.UUID, password: str) -> None:
        """Give an invited (password-less) user a password and confirm their address."""
        response = await _request_with_retry(
            self._client,
            "PUT",
            f"{self._base}/auth/v1/admin/users/{user_id}",
            json={"password": password, "email_confirm": True},
            headers=self._admin_headers(),
        )
        if response.status_code >= 400:
            raise SeedError(f"Auth set_password({user_id}) failed: {_api_error(response)}")

    async def delete_user(self, user_id: uuid.UUID) -> None:
        response = await _request_with_retry(
            self._client,
            "DELETE",
            f"{self._base}/auth/v1/admin/users/{user_id}",
            headers=self._admin_headers(),
        )
        if response.status_code >= 400 and response.status_code != 404:
            raise SeedError(f"Auth delete_user({user_id}) failed: {_api_error(response)}")

    async def sign_in(self, email: str, password: str) -> str:
        """Password grant — exactly what the frontend does. Returns the access token."""
        response = await _request_with_retry(
            self._client,
            "POST",
            f"{self._base}/auth/v1/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
            headers={"apikey": self._anon_key, "Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            raise SeedError(
                f"Sign-in as {email} failed: {_api_error(response)}\n"
                "If this is a 401 on the apikey header, set SUPABASE_ANON_KEY to the "
                "project's publishable key."
            )
        return response.json()["access_token"]


@dataclass
class Actor:
    """A seeded user plus the credentials needed to act as them."""

    spec: UserSpec
    org_slug: str
    email: str
    user_id: uuid.UUID
    token: str

    @property
    def label(self) -> str:
        return f"{self.org_slug}/{self.spec.slug}"


class FlowDeskApi:
    """Authenticated calls against the deployed FlowDesk API."""

    def __init__(self, client: httpx.AsyncClient, *, base: str, auth: SupabaseAuth, password: str):
        self._client = client
        self._root = base.rstrip("/")
        self._base = self._root + "/api/v1"
        self._auth = auth
        self._password = password

    async def health(self) -> dict:
        """`GET /health` — unauthenticated and outside the /api/v1 prefix."""
        response = await _request_with_retry(self._client, "GET", f"{self._root}/health")
        if response.status_code != 200:
            raise SeedError(f"GET {self._root}/health: {_api_error(response)}")
        return response.json()

    async def call(
        self,
        method: str,
        path: str,
        *,
        actor: Actor | None = None,
        json: dict | None = None,
        params: dict | None = None,
        expect: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        headers = {"Authorization": f"Bearer {actor.token}"} if actor else {}
        response = await _request_with_retry(
            self._client,
            method,
            f"{self._base}{path}",
            json=json,
            params=params,
            headers=headers,
        )
        # Supabase access tokens live an hour; a large run can outlast one. Re-sign-in once
        # rather than failing 300 requests in.
        if response.status_code == 401 and actor is not None:
            actor.token = await self._auth.sign_in(actor.email, self._password)
            response = await _request_with_retry(
                self._client,
                method,
                f"{self._base}{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {actor.token}"},
            )
        if response.status_code not in expect:
            who = actor.label if actor else "anonymous"
            raise SeedError(f"{method} {path} as {who}: {_api_error(response)}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()


# ---- Run state --------------------------------------------------------------------


@dataclass
class OrgRuntime:
    spec: OrgSpec
    tenant_id: uuid.UUID
    actors: dict[str, Actor] = field(default_factory=dict)
    category_ids: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def admin(self) -> Actor:
        return self.actors[self.spec.admin.slug]


@dataclass
class ExecutedIncident:
    plan: IncidentPlan
    incident_id: uuid.UUID
    #: `to_status` -> transition id, taken from the API's own detail payload.
    transition_ids: dict[str, uuid.UUID] = field(default_factory=dict)


# ---- Phase 0: preflight -----------------------------------------------------------


async def preflight(
    api: FlowDeskApi, auth: SupabaseAuth, conn, *, api_base: str
) -> list[dict]:
    heading("Phase 0 — preflight")
    health = await api.health()
    if health.get("status") != "ok":
        raise SeedError(f"{api_base}/health did not report ok: {health!r}")
    step(f"API healthy: {api_base}")

    version = await seed_db.schema_version(conn)
    step(f"alembic head: {version}")
    if version is None:
        raise SeedError("alembic_version is empty — the database has no schema.")

    auth_users = await auth.list_users()
    step(f"Supabase auth users: {len(auth_users)}")
    if len(auth_users) >= AUTH_USER_SOFT_LIMIT:
        raise SeedError(
            f"{len(auth_users)} auth users is at or above the safe limit of "
            f"{AUTH_USER_SOFT_LIMIT}. app/services/supabase_admin.get_user_by_email reads "
            "only the first 50, so the invite recovery path this seeder relies on would "
            "silently fail. Remove unused auth users first."
        )
    return auth_users


# ---- Phase 1: purge ---------------------------------------------------------------


async def purge(auth: SupabaseAuth, conn, *, auth_users: list[dict], dry_run: bool) -> None:
    heading("Phase 1 — purge previous demo data")
    names = seed_fixtures.demo_tenant_names()
    tenants = await seed_db.tenant_ids_by_name(conn, names)
    tenant_ids = list(tenants.values())

    emails = {e.lower() for e in seed_fixtures.demo_emails()}
    stale_auth = [u for u in auth_users if (u.get("email") or "").lower() in emails]

    preview = await seed_db.purge_preview(conn, tenant_ids)
    for table, count in preview.items():
        step(f"{table:<22} {count}")
    step(f"{'supabase auth users':<22} {len(stale_auth)}")

    if not tenant_ids and not stale_auth:
        step("nothing to purge")
        return
    if dry_run:
        warn("dry-run: nothing deleted")
        return

    deleted = await seed_db.purge(conn, tenant_ids)
    step("deleted: " + ", ".join(f"{t}={n}" for t, n in deleted.items()))
    for user in stale_auth:
        await auth.delete_user(uuid.UUID(user["id"]))
    step(f"deleted {len(stale_auth)} supabase auth user(s)")


# ---- Phase 2: organisations and users ---------------------------------------------


async def provision_org(
    api: FlowDeskApi, auth: SupabaseAuth, spec: OrgSpec, *, password: str
) -> OrgRuntime:
    admin_spec = spec.admin
    admin_email = admin_spec.email(spec.slug)

    # The one unavoidable invite email of this organisation.
    created = await api.call(
        "POST",
        "/organizations",
        json={
            "organization_name": spec.name,
            "admin_email": admin_email,
            "admin_name": admin_spec.name,
        },
        expect=(201,),
    )
    tenant_id = uuid.UUID(created["tenant"]["id"])
    admin_id = uuid.UUID(created["admin_user"]["id"])
    step(f"registered {spec.name} (tenant {tenant_id}) — 1 invite email sent")

    await auth.set_password(admin_id, password)
    admin = Actor(
        spec=admin_spec,
        org_slug=spec.slug,
        email=admin_email,
        user_id=admin_id,
        token=await auth.sign_in(admin_email, password),
    )
    org = OrgRuntime(spec=spec, tenant_id=tenant_id, actors={admin_spec.slug: admin})

    for user_spec in spec.users:
        if user_spec.role is Role.tenant_admin:
            continue
        email = user_spec.email(spec.slug)
        # Auth first, with a password and no email. POST /users then takes
        # user_service.invite_user's SupabaseUserExistsError branch and provisions the row
        # against this exact id.
        auth_id = await auth.create_user(email=email, password=password, name=user_spec.name)
        provisioned = await api.call(
            "POST",
            "/users",
            actor=admin,
            json={"email": email, "name": user_spec.name, "role": user_spec.role.value},
            expect=(201,),
        )
        row_id = uuid.UUID(provisioned["id"])
        if row_id != auth_id:
            raise SeedError(
                f"{email}: users.id {row_id} != auth id {auth_id}. The invite recovery "
                "path did not resolve the existing account; aborting before any incident "
                "is attributed to the wrong identity."
            )
        org.actors[user_spec.slug] = Actor(
            spec=user_spec,
            org_slug=spec.slug,
            email=email,
            user_id=row_id,
            token=await auth.sign_in(email, password),
        )
        step(f"provisioned {user_spec.role.value:<13} {user_spec.name} <{email}>")

    for category in spec.categories:
        row = await api.call(
            "POST",
            "/categories",
            actor=admin,
            json={"name": category.name, "description": category.description},
            expect=(201,),
        )
        org.category_ids[category.name] = uuid.UUID(row["id"])
    step(f"created {len(org.category_ids)} categories")
    return org


# ---- Phase 4: incidents -----------------------------------------------------------


async def execute_incident(
    api: FlowDeskApi, org: OrgRuntime, plan: IncidentPlan
) -> ExecutedIncident:
    """Walk one incident through the API in the order the state machine allows."""
    submitter = org.actors[plan.submitter_slug]
    detail = await api.call(
        "POST",
        "/incidents",
        actor=submitter,
        json={
            "title": plan.title,
            "description": plan.description,
            "category_id": str(org.category_ids[plan.category_name]),
            "severity": plan.severity.value,
        },
        expect=(201,),
    )
    executed = ExecutedIncident(plan=plan, incident_id=uuid.UUID(detail["id"]))

    if plan.reviewer_slug is not None:
        # A reviewer moving an unassigned incident to In Review also CLAIMS it
        # (workflow_service.transition), which is how assignment normally arises.
        detail = await api.call(
            "POST",
            f"/incidents/{executed.incident_id}/transitions",
            actor=org.actors[plan.reviewer_slug],
            json={"to_status": IncidentStatus.in_review.value, "note": plan.review_note},
            expect=(200,),
        )

    if plan.reassign_to_slug is not None:
        detail = await api.call(
            "POST",
            f"/incidents/{executed.incident_id}/assign",
            actor=org.admin,
            json={"assigned_to": str(org.actors[plan.reassign_to_slug].user_id)},
            expect=(200,),
        )

    if plan.closer_slug is not None:
        detail = await api.call(
            "POST",
            f"/incidents/{executed.incident_id}/transitions",
            actor=org.actors[plan.closer_slug],
            json={"to_status": IncidentStatus.closed.value, "note": plan.close_note},
            expect=(200,),
        )

    # Keyed by target state, not by position: transitions are ordered by
    # `(created_at, id)` and the id tiebreak is a random UUID, so position is not a safe
    # key. `to_status` is unique per incident in an acyclic open -> in_review -> closed.
    for transition in detail.get("transitions", []):
        executed.transition_ids[transition["to_status"]] = uuid.UUID(transition["id"])
    return executed


async def seed_incidents(
    api: FlowDeskApi, org: OrgRuntime, plans: list[IncidentPlan], *, concurrency: int
) -> list[ExecutedIncident]:
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def run(plan: IncidentPlan) -> ExecutedIncident:
        nonlocal done
        async with semaphore:
            result = await execute_incident(api, org, plan)
        done += 1
        if done % 10 == 0:
            step(f"{done}/{len(plans)} incidents")
        return result

    return list(await asyncio.gather(*(run(p) for p in plans)))


# ---- Phase 5: notification realism ------------------------------------------------


async def age_notifications(api: FlowDeskApi, orgs: list[OrgRuntime]) -> None:
    """Mark most notifications read, leaving a realistic unread badge (UC-09)."""
    for org in orgs:
        for actor in org.actors.values():
            page = await api.call(
                "GET", "/notifications", actor=actor, params={"limit": 100}, expect=(200,)
            )
            rows = page["data"]
            keep_unread = max(int(len(rows) * UNREAD_SHARE), 1 if rows else 0)
            # Newest first, so the unread ones are the recent ones — as they would be.
            for row in rows[keep_unread:]:
                await api.call(
                    "POST", f"/notifications/{row['id']}/read", actor=actor, expect=(200,)
                )
            step(f"{actor.label:<16} {len(rows)} notifications, {keep_unread} left unread")


# ---- Phase 6: backfill ------------------------------------------------------------


def _classify(message: str) -> str:
    """Map a notification message back to the event that produced it.

    Mirrors notification_service's templates: `'"{title}" is now {state}.'` for a
    transition and `'You have been assigned "{title}" ({state}).'` for a reassignment.
    """
    if message.startswith("You have been assigned"):
        return "reassigned"
    if message.endswith("is now In Review."):
        return "in_review"
    if message.endswith("is now Closed."):
        return "closed"
    return "unknown"


def build_stamps(
    executed: list[ExecutedIncident],
    notifications: dict[uuid.UUID, list[tuple[uuid.UUID, str]]],
) -> tuple[list[seed_db.IncidentStamp], list[seed_db.RowStamp], list[seed_db.RowStamp], list[str]]:
    incidents: list[seed_db.IncidentStamp] = []
    transitions: list[seed_db.RowStamp] = []
    notification_stamps: list[seed_db.RowStamp] = []
    problems: list[str] = []

    for item in executed:
        plan = item.plan
        incidents.append(
            seed_db.IncidentStamp(
                incident_id=item.incident_id,
                created_at=plan.created_at,
                updated_at=plan.updated_at,
            )
        )
        by_state = {
            IncidentStatus.in_review.value: plan.review_at,
            IncidentStatus.closed.value: plan.closed_at,
        }
        for to_status, transition_id in item.transition_ids.items():
            planned = by_state.get(to_status)
            if planned is None:
                problems.append(f"{item.incident_id}: unplanned transition to {to_status}")
                continue
            transitions.append(seed_db.RowStamp(row_id=transition_id, created_at=planned))

        event_times = {
            "in_review": plan.review_at,
            "reassigned": plan.reassign_at,
            "closed": plan.closed_at,
        }
        for row_id, message in notifications.get(item.incident_id, []):
            kind = _classify(message)
            planned = event_times.get(kind)
            if planned is None:
                # Never drop one: an unstamped notification keeps `now()` while its
                # incident moves into the past, which the backfill's own verification
                # would then reject. Pin it to the incident's last write instead.
                problems.append(f"{item.incident_id}: unmatched notification ({kind})")
                planned = plan.updated_at
            notification_stamps.append(
                seed_db.RowStamp(row_id=row_id, created_at=planned)
            )
    return incidents, transitions, notification_stamps, problems


async def backfill(
    conn, executed: list[ExecutedIncident], tenant_ids: list[uuid.UUID], *, dry_run: bool
) -> None:
    heading("Phase 6 — backfill timestamps (direct SQL)")
    incident_ids = [e.incident_id for e in executed]
    notifications = await seed_db.seeded_notifications(conn, incident_ids)
    incidents, transitions, notification_stamps, problems = build_stamps(
        executed, notifications
    )
    for problem in problems[:10]:
        warn(problem)
    if len(problems) > 10:
        warn(f"...and {len(problems) - 10} more")

    step(
        f"planned: {len(incidents)} incidents, {len(transitions)} transitions, "
        f"{len(notification_stamps)} notifications"
    )
    if dry_run:
        warn("dry-run: no timestamps written")
        return
    updated = await seed_db.backfill_timestamps(
        conn,
        incidents=incidents,
        transitions=transitions,
        notifications=notification_stamps,
        tenant_ids=tenant_ids,
    )
    step("updated: " + ", ".join(f"{t}={n}" for t, n in updated.items()))


# ---- Phase 7: verification --------------------------------------------------------


async def verify(
    api: FlowDeskApi, orgs: list[OrgRuntime], plans: list[OrgPlan], *, backfilled: bool
) -> None:
    heading("Phase 7 — verification (read-only, through the API)")
    failures: list[str] = []

    for org, plan in zip(orgs, plans):
        volume = await api.call("GET", "/analytics/volume", actor=org.admin, expect=(200,))
        buckets = [b for b in volume["data"] if b["count"] > 0]
        step(
            f"{org.spec.slug}: volume {len(buckets)}/{len(volume['data'])} non-empty weeks, "
            f"{sum(b['count'] for b in volume['data'])} incidents in window"
        )
        # Without the backfill every incident is dated today by the server, so a single
        # non-empty bucket is the correct outcome, not a failure.
        if backfilled and len(buckets) < 8:
            failures.append(
                f"{org.spec.slug}: only {len(buckets)} non-empty weekly buckets — the "
                "backfill did not spread the data."
            )

        distribution = await api.call(
            "GET", "/analytics/status-distribution", actor=org.admin, expect=(200,)
        )
        actual = {row["status"]: row["count"] for row in distribution["data"]}
        expected = {s.value: c for s, c in plan.status_counts.items()}
        step(f"{org.spec.slug}: status {actual} (planned {expected})")
        if actual != expected:
            failures.append(f"{org.spec.slug}: status distribution {actual} != {expected}")

        staff = org.actors[org.spec.staff[0].slug]
        own = await api.call(
            "GET", "/incidents", actor=staff, params={"limit": 100}, expect=(200,)
        )
        foreign = [
            r for r in own["data"] if r["submitted_by"]["id"] != str(staff.user_id)
        ]
        step(f"{org.spec.slug}: staff sees {own['pagination']['total']} own incidents")
        if foreign:
            failures.append(f"{org.spec.slug}: staff list leaked {len(foreign)} foreign rows")

        reviewer = org.actors[org.spec.reviewers[0].slug]
        queue = await api.call(
            "GET", "/incidents", actor=reviewer, params={"limit": 1}, expect=(200,)
        )
        step(f"{org.spec.slug}: reviewer queue {queue['pagination']['total']} incidents")

        for actor in org.actors.values():
            badge = await api.call(
                "GET", "/notifications/unread-count", actor=actor, expect=(200,)
            )
            if badge["unread"] == 0 and actor.spec.role is not Role.tenant_admin:
                warn(f"{actor.label}: unread badge is 0")

    if len(orgs) >= 2:
        # NFR-12: an id from another tenant must be indistinguishable from a missing one.
        other = await api.call(
            "GET", "/incidents", actor=orgs[1].admin, params={"limit": 1}, expect=(200,)
        )
        if other["data"]:
            foreign_id = other["data"][0]["id"]
            await api.call(
                "GET", f"/incidents/{foreign_id}", actor=orgs[0].admin, expect=(404,)
            )
            step("cross-tenant probe: 404 (NFR-12 holds)")
        else:
            warn("cross-tenant probe skipped: second organisation has no incidents")

    if failures:
        raise SeedError("Verification failed:\n  - " + "\n  - ".join(failures))
    info("\nAll verification checks passed.")


# ---- Reporting --------------------------------------------------------------------


def print_plan_summary(plans: list[OrgPlan]) -> None:
    heading("Plan")
    for plan in plans:
        counts = plan.status_counts
        first = min(i.created_at for i in plan.incidents).date()
        last = max(i.created_at for i in plan.incidents).date()
        reassignments = sum(1 for i in plan.incidents if i.reassign_to_slug)
        transitions = sum(len(i.transition_times) for i in plan.incidents)
        info(f"\n{plan.spec.name}")
        info(f"  users        {len(plan.spec.users)}  categories {len(plan.spec.categories)}")
        info(f"  incidents    {len(plan.incidents)}  ({first} .. {last})")
        info(
            "  statuses     "
            + ", ".join(f"{s.value}={counts[s]}" for s in IncidentStatus)
        )
        info(f"  transitions  {transitions}  reassignments {reassignments}")


def print_credentials(orgs: Iterable[OrgRuntime], password: str) -> None:
    heading("Demo credentials")
    info(f"{'organisation':<38} {'role':<14} {'email':<52} name")
    for org in orgs:
        for user_spec in org.spec.users:
            actor = org.actors.get(user_spec.slug)
            if actor is None:
                continue
            info(
                f"{org.spec.name:<38} {user_spec.role.value:<14} "
                f"{actor.email:<52} {user_spec.name}"
            )
    info(f"\nPassword for every account above: {password}")


# ---- Orchestration ----------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    password = os.environ.get("SEED_DEMO_PASSWORD", "").strip()
    if not password and not args.dry_run:
        raise SeedError(
            "SEED_DEMO_PASSWORD is not set. Choose a password for the demo accounts and "
            "export it; it is never stored in this repository."
        )
    if len(password) < 8 and not args.dry_run:
        raise SeedError("SEED_DEMO_PASSWORD must be at least 8 characters (Supabase minimum).")

    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip() or settings.supabase_service_role_key
    if not settings.supabase_service_role_key:
        raise SeedError("SUPABASE_SERVICE_ROLE_KEY is not configured (.env).")

    tz = ZoneInfo(settings.reporting_timezone)
    now = datetime.now(tz)
    plans = seed_fixtures.build_plan(seed=args.random_seed, now=now, tz=tz)

    db_host = settings.database_url.rsplit("@", 1)[-1]
    heading("Target")
    info(f"  API        {args.api_base}")
    info(f"  Supabase   {settings.supabase_url}")
    info(f"  Database   {db_host}")
    info(f"  Timezone   {settings.reporting_timezone}  (now {now:%Y-%m-%d %H:%M})")
    info(f"  Seed       {args.random_seed}")
    print_plan_summary(plans)

    if not args.yes and not args.dry_run:
        info("")
        answer = input("This writes to the target above. Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            info("Aborted.")
            return 1

    conn = await seed_db.connect(settings.database_url)
    timeout = httpx.Timeout(30.0, connect=15.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            auth = SupabaseAuth(
                client,
                base=settings.supabase_url,
                service_key=settings.supabase_service_role_key,
                anon_key=anon_key,
            )
            api = FlowDeskApi(client, base=args.api_base, auth=auth, password=password)

            auth_users = await preflight(api, auth, conn, api_base=args.api_base)

            if args.purge:
                await purge(auth, conn, auth_users=auth_users, dry_run=args.dry_run)

            if not args.seed:
                if args.backfill:
                    warn("--backfill without --seed has no incident ids to work from; "
                         "run --seed and --backfill together.")
                return 0

            heading("Phase 2-3 — organisations, users, categories")
            if args.dry_run:
                warn("dry-run: no organisations, users, categories or incidents created")
                return 0

            orgs: list[OrgRuntime] = []
            executed: list[ExecutedIncident] = []
            for index, plan in enumerate(plans):
                if index:
                    # Space the invite emails out: GoTrue's built-in SMTP rate limits, and
                    # a 429 here costs the whole organisation.
                    step(f"waiting {args.invite_gap}s before the next invite email")
                    await asyncio.sleep(args.invite_gap)
                orgs.append(await provision_org(api, auth, plan.spec, password=password))

            heading("Phase 4 — incidents and workflow")
            for org, plan in zip(orgs, plans):
                step(f"{org.spec.name}: {len(plan.incidents)} incidents")
                executed.extend(
                    await seed_incidents(
                        api, org, plan.incidents, concurrency=args.concurrency
                    )
                )

            heading("Phase 5 — notification panel state")
            await age_notifications(api, orgs)

            if args.backfill:
                await backfill(
                    conn, executed, [o.tenant_id for o in orgs], dry_run=args.dry_run
                )

            await verify(api, orgs, plans, backfilled=args.backfill)
            print_credentials(orgs, password)
    finally:
        await conn.close()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.seed_demo",
        description="Seed the deployed FlowDesk instance with demo data through its API.",
    )
    parser.add_argument("--all", action="store_true", help="purge, seed and backfill")
    parser.add_argument("--purge", action="store_true", help="delete previous demo data")
    parser.add_argument("--seed", action="store_true", help="create data through the API")
    parser.add_argument(
        "--backfill", action="store_true", help="rewrite timestamps across the window"
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--invite-gap",
        type=int,
        default=20,
        help="seconds to wait between organisation registrations (each sends one email)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args(argv)
    if args.all:
        args.purge = args.seed = args.backfill = True
    if not (args.purge or args.seed or args.backfill):
        parser.error("choose at least one of --all, --purge, --seed, --backfill")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(run(args))
    except (SeedError, seed_db.SeedDbError) as exc:
        info(f"\nERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        info("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
