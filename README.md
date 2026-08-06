# FlowDesk — Backend

Multi-tenant SaaS incident & workflow management platform (SDM404, Sprint 1 backend).
FastAPI + Supabase (Auth + Postgres) + Fly.io. This service is the **single point of
enforcement** for authentication, RBAC and tenant isolation: the React frontend obtains a
JWT from Supabase and sends it in the `Authorization` header, and this API verifies it on
every request.

## Stack

- **FastAPI** on Fly.io (Sydney, `ap-southeast-2`)
- **Supabase**: GoTrue Auth (email/password) + managed PostgreSQL
- **SQLAlchemy 2.0 (async) + asyncpg + Alembic**
- JWT verified **asymmetrically via the Supabase JWKS endpoint** (RS256/ES256), not the
  legacy HS256 secret

## Authentication

**The backend has no login endpoint.** The frontend authenticates against **Supabase**,
receives a JWT, and sends it to this API — which only *verifies* it. Credentials never
reach the backend; role and tenant come from the database (via `GET /api/v1/me`), not the
token.

![Where authentication happens](docs/auth_flow.png)

1. The frontend signs in at Supabase (`POST /auth/v1/token?grant_type=password`, or
   `supabase.auth.signInWithPassword()`), which returns an `access_token` (JWT, ES256, 1 h).
2. The frontend calls this API with `Authorization: Bearer <access_token>`; the API
   verifies the signature against Supabase's JWKS endpoint on every request.

### Frontend (supabase-js)

```js
import { createClient } from '@supabase/supabase-js'

// one shared client; the anon key is public and safe in the browser
export const supabase = createClient(
  'https://tqkyghashkawgqbnrwaf.supabase.co',
  'sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24',
  { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } }
)

// log in
const { data, error } = await supabase.auth.signInWithPassword({ email, password })

// call the backend with the current token
async function api(path, init = {}) {
  const { data: { session } } = await supabase.auth.getSession()
  return fetch(`https://flowdesk-backend.fly.dev/api/v1${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init.headers || {}),
               Authorization: `Bearer ${session?.access_token ?? ''}` },
  })
}
const me = await api('/me').then((r) => r.json())
```

supabase-js persists and auto-refreshes the session. On a `401` with
`details.reason == "token_expired"`, call `supabase.auth.refreshSession()` and retry once.
New users have no password until they follow the Supabase invite/reset email
(`supabase.auth.updateUser({ password })`).

### Get a token with curl (testing)

```bash
TOKEN=$(curl -s "https://tqkyghashkawgqbnrwaf.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"your-password"}' | jq -r .access_token)

curl https://flowdesk-backend.fly.dev/api/v1/me -H "Authorization: Bearer $TOKEN"
```

The user needs an existing password — set via the invite email, or created in the Supabase
Dashboard (Authentication → Users → Add user, "Auto Confirm User"). The full per-endpoint
contract is maintained as a separate Word document (`FlowDesk_API_Contract.docx`), shared
with the team.

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                       # create .venv and install deps (incl. dev group)
cp .env.example .env          # fill in Supabase + database values
uv run uvicorn app.main:app --reload
```

Interactive API docs (the living contract for the frontend): http://localhost:8000/docs

### Database migrations

```bash
uv run alembic upgrade head       # apply all migrations
uv run alembic downgrade base     # roll back
```

### Tests

Tests run against a real PostgreSQL. Start one and point `TEST_DATABASE_URL` at it:

```bash
docker run -d --name flowdesk-test-pg -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=flowdesk_test -p 5433:5432 postgres:16

export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5433/flowdesk_test
uv run pytest -q
uv run flake8 app tests
```

## Demo data seeding

[`scripts/seed_demo.py`](scripts/seed_demo.py) populates a deployed environment with two
demonstration organisations — 5 users, 6 categories and 40 incidents each, spread over the
last 12 weeks. It drives the **public API**, not the database, so the workflow state
machine, the notification hooks, RBAC and tenant scoping all execute normally and the
resulting data doubles as evidence that the API works.

```bash
export SEED_DEMO_PASSWORD='<password for the demo accounts>'

uv run python -m scripts.seed_demo --all --dry-run   # print the plan, write nothing
uv run python -m scripts.seed_demo --all --yes       # purge + seed + backfill
```

Phases are individually selectable (`--purge`, `--seed`, `--backfill`); `--all` runs all
three. `--random-seed` makes a run reproducible — the same seed produces the same
organisations, incidents and timestamps.

Two operations cannot go through the API and are confined to
[`scripts/seed_db.py`](scripts/seed_db.py):

- **Purge.** Nothing in the product deletes a tenant, user or incident, so re-running needs
  SQL. It matches tenants by *exact name* and auth users by *exact address* from the
  fixture catalogue — never a wildcard — so accounts the seeder did not create cannot be
  caught by it.
- **Timestamps.** `created_at` is `server_default=now()`; no request can set it. Without a
  backfill every incident lands in the current week and `/analytics/volume` renders one
  bar. The pass verifies its own postconditions inside the transaction and rolls back on
  any violation.

Provisioning users is deliberately a hybrid. `users.id` *is* the Supabase Auth id, and the
only endpoints that create one (`POST /organizations`, `POST /users`) both call GoTrue
invite — which sends an email, and the built-in SMTP allows only a couple per hour. Staff
and reviewers are therefore created in Auth first (confirmed, with a password, **no
email**) and then through `POST /api/v1/users`, which takes `user_service.invite_user`'s
existing `SupabaseUserExistsError` recovery branch and provisions the row against that id.
`register_organization` has no such branch, so **each organisation costs exactly one invite
email** — two per run, spaced by `--invite-gap` seconds.

Tokens come from Supabase's password grant, exactly as the frontend gets them. Set
`SUPABASE_ANON_KEY` to the project's publishable key if the `apikey` header is rejected;
otherwise the service-role key from `.env` is used.

## API surface

Base path `/api/v1`. JWT required on all endpoints except `POST /organizations` and
`GET /health`. Errors use one envelope: `{"error": {"code", "message", "details"}}` — with the
documented exceptions in [the contract](docs/api-contract.md#314-responses-that-are-not-in-the-envelope).

> **The canonical API contract is [`docs/api-contract.md`](docs/api-contract.md).** It documents
> every endpoint, every error slug and its trigger, the RBAC and tenant-scoping rules, and the
> domain semantics that `/openapi.json` cannot express. Read it before wiring a client — in
> particular §3.1.4, because the error envelope is *not* universal. The table below is a summary
> only; `docs/api-contract-sprint2.md` and `docs/api-contract-sprint3.md` are superseded and kept
> for history.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | public | Liveness probe |
| GET | `/api/v1/me` | any | Resolve caller identity, role, tenant |
| POST | `/api/v1/organizations` | public | Register org + first Tenant Admin (UC-02) |
| GET/POST | `/api/v1/categories` | read: any / write: tenant_admin | Categories (UC-04) |
| GET/PATCH/DELETE | `/api/v1/categories/{id}` | write: tenant_admin | Category detail |
| GET/POST | `/api/v1/users` | tenant_admin / system_admin | User management (UC-03) |
| GET/PATCH | `/api/v1/users/{id}` | admin | User detail / edit role |
| POST | `/api/v1/users/{id}/deactivate` \| `/activate` | admin | Toggle status |
| POST | `/api/v1/incidents` | staff | Submit an incident (UC-06, US-08) |
| GET | `/api/v1/incidents` | any (role-scoped) | List; filter `status`/`severity`, sort, paginate (UC-07, US-09) |
| GET | `/api/v1/incidents/{id}` | any (role-scoped) | Detail + workflow timeline (UC-11, US-12) |
| GET | `/api/v1/incidents/{id}/transitions` | any (role-scoped) | Timeline only (UC-11) |
| POST | `/api/v1/incidents/{id}/transitions` | reviewer / tenant_admin | Workflow transition, idempotent (UC-08, US-10/11) |
| POST | `/api/v1/incidents/{id}/assign` | tenant_admin | Reassign reviewer (UC-08 A1) |
| GET | `/api/v1/notifications` | any (recipient-scoped) | Notification panel, newest first (UC-09, US-14) |
| GET | `/api/v1/notifications/unread-count` | any | Bell badge (UC-09 step 3) |
| POST | `/api/v1/notifications/{id}/read` | recipient only | Mark one read (UC-09 steps 6-7) |
| POST | `/api/v1/notifications/read-all` | any | Dismiss all (UC-09 A1) |
| GET | `/api/v1/analytics/volume` | tenant_admin / system_admin | Incidents per week (UC-10, US-15) |
| GET | `/api/v1/analytics/status-distribution` | tenant_admin / system_admin | Current status split (UC-10, US-16) |

Nothing is reserved any more: `routes/reserved.py` was deleted when US-15/16 shipped.
`POST /notifications` and `POST /analytics/*` now answer `405 method_not_allowed` —
notifications are system-generated and analytics are reads.

### Workflow (UC-08)

The only legal edges are `open -> in_review -> closed`. A direct `open -> closed` is
refused with `409 invalid_transition` (UC-08 E1); closing without a resolution note is
refused with `422 resolution_note_required` (UC-08 E2).

Transitions are **idempotent in effect though POST in method** (NFR-06): idempotency is
defined over the postcondition, not the request. Asking for a state the incident already
occupies returns `200` with the current state and writes nothing — no duplicate timeline
row, no duplicate audit line, no notification. `open` is never the target of a legal edge,
so `open -> open` is E1 rather than a replay. See `app/services/workflow_service.py`.

Reviewers work from a shared queue: `GET /incidents` shows a reviewer the incidents
assigned to them **plus** the unassigned ones, and moving an unassigned incident to
`in_review` claims it. Without this, UC-07's `assigned_to == me` scope combined with
UC-06's unassigned creation would leave every new incident invisible to every reviewer.

`GET /incidents/{id}` returns `allowed_transitions` — the states **this caller** may move
the incident to — so the frontend never re-implements the state machine. It is
caller-dependent and must not be cached across users. A freshly submitted incident has an
empty `transitions` array: `workflow_transitions` records state changes only, so there is
no synthetic "created" event and reassignment writes no row there either.

### Notifications (UC-09)

A real state change notifies the incident's **submitter**; a reassignment notifies the
**new reviewer**. Replays, rejected transitions and the caller's own actions notify nobody.

Delivery happens inside the transition's transaction, so a notification that succeeds is
atomic with the state change that caused it. UC-09 E1 requires the reverse guarantee — a
delivery failure must not cost the user their state change — so the insert runs inside a
`SAVEPOINT` and an unavailable recipient is checked for first. Either way the failure is a
logged no-op (`flowdesk.audit`, `notification_skipped`) and the transition stands. A second
transaction after the commit was rejected: it would leave a window where the transition is
durable and the notification is lost for good.

Notifications are personal correspondence, not tenant data: the only visibility rule is
`user_id == caller`, with no cross-tenant exemption for System Admin. Anything else is
`404 notification_not_found`, never `403`.

### Analytics (UC-10)

`GET /analytics/volume` buckets incidents by ISO week (Monday-based) **in
`REPORTING_TIMEZONE`**, not in UTC. This is load-bearing: `created_at` is `timestamptz` and
the Fly machine clock is UTC, so an incident submitted Monday 09:00 Melbourne is Sunday
23:00 UTC and a naive `date_trunc('week', created_at)` files it a week early — silently,
every Monday morning. The response echoes the `timezone` it used.

Every week in the requested range is returned, including zero-count ones, so the frontend
never has to generate the missing Mondays in the *browser's* timezone. The window defaults
to the last 12 weeks, snaps outward to whole weeks (and echoes the effective `from`/`to`),
and is capped at 53 weeks — `422 date_range_too_large` beyond that.

An organisation with no incidents is a `200` with zero-filled buckets and `total: 0`,
never a `404`. Both endpoints are `tenant_admin`/`system_admin` only: they aggregate every
incident in the tenant, which is strictly more than a staff or reviewer caller may read.

## Deployment

Two separate managed services, one live URL:

- **Fly.io** (Sydney, `ap-southeast-2`) runs the FastAPI container — the app.
- **Supabase** (Sydney) provides Auth (GoTrue) and the managed **PostgreSQL** — the database.

Fly does **not** host the database. The app reaches Supabase Postgres over the network
using `DATABASE_URL`. Live app: `https://flowdesk-backend.fly.dev`.

### What a deploy does

A push to `main` triggers [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml),
which runs `flyctl deploy --remote-only`. That single command performs four steps:

1. **Build** — the Docker image is built on Fly's remote builders and pushed to the Fly
   registry (no local Docker needed).
2. **Release command** — Fly boots a short-lived **release machine** from the new image,
   with all app secrets injected as env vars, and runs the `release_command` from
   [`fly.toml`](fly.toml): `alembic upgrade head`. **This is where the schema change is
   applied to Supabase** (see below). The release machine is destroyed afterwards.
3. **Fail-safe gate** — if the migration exits non-zero, Fly **aborts the release**. The
   currently-running version keeps serving; there is no downtime and users never see a
   half-migrated schema.
4. **Rollout** — on success, Fly rolls the new version onto the app machines
   (rolling strategy, each health-checked on `/health` before taking traffic).

```mermaid
sequenceDiagram
    participant Dev as git push main
    participant GA as GitHub Actions (deploy.yml)
    participant Fly as Fly.io
    participant Rel as Release machine (ephemeral)
    participant SB as Supabase Postgres (Sydney)
    participant App as App machines (2×, Sydney)
    Dev->>GA: push
    GA->>Fly: flyctl deploy --remote-only
    Fly->>Fly: build image on remote builder
    Fly->>Rel: start release machine (prod secrets)
    Rel->>SB: alembic upgrade head  (via DATABASE_URL)
    SB-->>Rel: schema at head
    Note over Rel,Fly: non-zero exit → deploy ABORTED, old version stays live
    Rel-->>Fly: success, machine destroyed
    Fly->>App: rolling update, health-check /health
```

### How migrations reach Supabase

This is the key detail. Migrations are **not** run by hand in the Supabase SQL editor and
**not** run from a laptop in the normal flow — they run inside Fly's ephemeral release
machine, which happens to hold the production secrets:

- [`alembic/env.py`](alembic/env.py) builds an async engine from
  `settings.database_url` (i.e. the `DATABASE_URL` env var) and connects **out to Supabase
  Postgres**. [`alembic.ini`](alembic.ini) leaves `sqlalchemy.url` blank on purpose, so no
  DB credentials ever live in version control.
- `DATABASE_URL` targets the Supabase **session pooler**, e.g.
  `postgresql+asyncpg://postgres.<ref>:<pw>@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres`.
  The pooler host is used (rather than the direct `db.<ref>.supabase.co`) because the
  direct host is **IPv6-only** and unresolvable from many networks (local dev, some CI
  runners); the pooler is reachable over IPv4 from Fly, GitHub Actions, and laptops alike.
- Because that URL points at Supabase, `alembic upgrade head` — whether run by the Fly
  release command, in CI, or locally — always operates on the database at the end of that
  URL. In production that is Supabase.

### Adding a migration (normal workflow)

```bash
uv run alembic revision -m "add something"        # or --autogenerate
# edit the generated file in alembic/versions/, then commit it
git commit -am "AB#NN: migration — add something"
```

On the next push to `main`, the deploy's `release_command` applies it to Supabase
automatically. You do not touch the Supabase SQL editor and do not run anything by hand.

### Running or inspecting migrations manually

Occasionally you may want to check or force state. Prefer doing it **from a Fly machine**,
which already has the production secrets (nothing to copy to your laptop):

```bash
flyctl ssh console -a flowdesk-backend -C "alembic current"      # show applied revision
flyctl ssh console -a flowdesk-backend -C "alembic upgrade head" # apply pending
flyctl ssh console -a flowdesk-backend -C "alembic history"      # list migrations
```

Alternatively, from a laptop pointed at the prod DB (use with care — this writes to
production data):

```bash
DATABASE_URL="postgresql+asyncpg://postgres.<ref>:<pw>@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres" \
  uv run alembic current
```

### Rollback

```bash
flyctl ssh console -a flowdesk-backend -C "alembic downgrade -1"   # one step back
```

Redeploying an older image does **not** auto-downgrade the database — Alembic only moves
forward during a deploy. If a release must be undone at the schema level, downgrade
explicitly; in practice prefer a new forward-fixing migration.

### CI safety net

Every PR runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml), which spins up a
throwaway PostgreSQL 16 and runs `alembic upgrade head` **and** `alembic downgrade base`.
A migration that cannot apply or revert cleanly fails CI and never reaches `main` — so it
never reaches the Supabase release step.

### First-time setup (one-off)

```bash
flyctl apps create flowdesk-backend --org personal

flyctl secrets set --app flowdesk-backend \
  SUPABASE_URL="https://<ref>.supabase.co" \
  SUPABASE_PROJECT_REF="<ref>" \
  SUPABASE_SERVICE_ROLE_KEY="sb_secret_..." \
  SUPABASE_JWKS_URL="https://<ref>.supabase.co/auth/v1/.well-known/jwks.json" \
  JWT_AUDIENCE="authenticated" \
  JWT_ISSUER="https://<ref>.supabase.co/auth/v1" \
  DATABASE_URL="postgresql+asyncpg://postgres.<ref>:<pw>@aws-1-ap-southeast-2.pooler.supabase.com:5432/postgres" \
  CORS_ORIGINS="http://localhost:5173,https://flowdesk.vanelsen.net.au"
```

`APP_ENV`, `PORT` and `REPORTING_TIMEZONE` come from `[env]` in `fly.toml`, not from
secrets — none of them is sensitive. `REPORTING_TIMEZONE` is an IANA name validated at
start-up, so a typo is a refusal to boot rather than a per-request analytics error; the
`tzdata` package is a runtime dependency for exactly that reason, since `python:3.12-slim`
does not guarantee an OS tzdb. Add `FLY_API_TOKEN` to the GitHub repo's **Actions
secrets** so `deploy.yml` can authenticate.
`.env` is git-ignored; a `.dockerignore` keeps it (and other cruft) out of the image.

## Contributing (Appendix B coding standards)

- PEP 8, enforced by `flake8` in CI; 4-space indent; business logic in `app/services/`.
- No secrets committed; all config via env vars (`.env` is git-ignored).
- **Every commit references its Azure DevOps work item**, e.g. `AB#123: verify Supabase JWT`.
  The GitHub repo is linked to the Azure Boards project
  [`FadyTadros/FlowDesk-SDM404`](https://dev.azure.com/FadyTadros/FlowDesk-SDM404) via the
  Azure Boards app so `AB#<id>` mentions auto-link and update work items.
- Every PR needs one peer approval before merge to `main`.
