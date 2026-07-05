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

Push to `main` triggers `.github/workflows/deploy.yml`:
`flyctl deploy` builds the image, runs `alembic upgrade head` via `release_command`, then
releases. Set secrets once:

```bash
flyctl secrets set SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
  SUPABASE_JWKS_URL=... JWT_ISSUER=... DATABASE_URL=...
```

## Contributing (Appendix B coding standards)

- PEP 8, enforced by `flake8` in CI; 4-space indent; business logic in `app/services/`.
- No secrets committed; all config via env vars (`.env` is git-ignored).
- **Every commit references its Azure DevOps work item**, e.g. `AB#123: verify Supabase JWT`.
  The GitHub repo is linked to the Azure Boards project
  [`FadyTadros/FlowDesk-SDM404`](https://dev.azure.com/FadyTadros/FlowDesk-SDM404) via the
  Azure Boards app so `AB#<id>` mentions auto-link and update work items.
- Every PR needs one peer approval before merge to `main`.
