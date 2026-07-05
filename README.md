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

## API surface (Sprint 1)

Base path `/api/v1`. JWT required on all endpoints except `POST /organizations` and
`GET /health`. Errors use one envelope: `{"error": {"code", "message", "details"}}`.

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

Sprint 2/3 endpoints (`/incidents`, `/incidents/{id}/transitions`, `/notifications`,
`/analytics/*`) are **reserved** and return `501` until built, so the contract is stable.

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

`APP_ENV` and `PORT` come from `[env]` in `fly.toml`, not from secrets. Add
`FLY_API_TOKEN` to the GitHub repo's **Actions secrets** so `deploy.yml` can authenticate.
`.env` is git-ignored; a `.dockerignore` keeps it (and other cruft) out of the image.

## Contributing (Appendix B coding standards)

- PEP 8, enforced by `flake8` in CI; 4-space indent; business logic in `app/services/`.
- No secrets committed; all config via env vars (`.env` is git-ignored).
- **Every commit references its Azure DevOps work item**, e.g. `AB#123: verify Supabase JWT`.
  The GitHub repo is linked to the Azure Boards project
  [`FadyTadros/FlowDesk-SDM404`](https://dev.azure.com/FadyTadros/FlowDesk-SDM404) via the
  Azure Boards app so `AB#<id>` mentions auto-link and update work items.
- Every PR needs one peer approval before merge to `main`.
