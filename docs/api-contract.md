# FlowDesk API Contract — v2.0 (complete)

**For:** Bradley Van Elsen (Frontend Lead), Fady Tadros (Product Owner) · **From:** Ivan Bazhenov (Backend Lead)
**Covers:** the entire shipped backend — Sprints 1, 2 and 3 (UC-01 … UC-11, US-01 … US-16)
**Status:** implemented, merged to `main`, deployed. Verified against the live deployment on 30 July 2026.
**Supersedes:** `FlowDesk_API_Contract.docx` v1.0, `api-contract-sprint2.md`, `api-contract-sprint3.md`.

This is the single canonical contract. It replaces the three documents above — they were a
Sprint-1 base plus two deltas, which meant answering "what does this endpoint do?" required
reading three files and knowing which one won. Everything is stated once, here.

It is markdown rather than Word so it can be reviewed in the same pull request that changes
the API; a `.docx` is binary and cannot be diffed. **Proposal for Fady:** make this file
canonical and generate the Word document from it when a Word copy is needed for submission.

Every claim below was taken from the source code, and the numbers, slugs and message strings
were cross-checked by a second pass whose only job was to find claims the first pass got
wrong. Where the implementation contradicts an earlier version of this contract, §7.1 says so
explicitly rather than quietly correcting it.

---

## 1. Overview

### 1.1 Architecture

FlowDesk is a multi-tenant SaaS incident and workflow platform. Two managed services back the
product, and the React frontend talks to both:

- **Backend** — FastAPI on Fly.io (Sydney, `ap-southeast-2`). This is the single point of
  enforcement: it verifies every JWT, applies RBAC, enforces tenant isolation, and is the only
  component that reads or writes the database.
- **Supabase** — GoTrue Auth (email/password) + managed PostgreSQL. The frontend authenticates
  directly against Supabase; **the backend never sees passwords.**
- **Frontend** — React + Tailwind (Brad), hosted at `https://flowdesk.vanelsen.net.au`. It
  obtains a JWT from Supabase and sends it to the FlowDesk API on every call.

Transport is JSON (`application/json`), UTF-8, throughout.

### 1.2 How to use this document — and a warning about `/openapi.json`

The live OpenAPI document at `/openapi.json` (rendered at `/docs`) is authoritative for
**request and response shapes**. It is safe to code-generate a typed client from, with two
corrections you must apply by hand:

1. **It documents only the success codes and `422`.** `401`, `403`, `404`, `405` and `409` are
   absent from every operation. A generated client will have no types for them.
2. **The `422` schema it advertises is wrong.** OpenAPI declares FastAPI's default
   `HTTPValidationError`, i.e. `{"detail": [...]}`. The application does not return that — it
   returns the shared envelope `{"error": {"code", "message", "details"}}` with the pydantic
   list under `error.details.errors`. Do not type your error handler from the generated schema;
   type it from §3.1.
3. **Four operations declare no error response at all.** `GET /me`, `GET /health`,
   `GET /notifications/unread-count` and `POST /notifications/read-all` have no parameters and
   no body, so FastAPI emits only `200` for them. A generated client gets *no* error type for
   `GET /me` — the endpoint your login flow depends on.

That gap is exactly what this document exists to fill: the runtime error behaviour, the RBAC
rules, and the domain semantics that no schema can express.

### 1.3 Environments

| | Value |
|---|---|
| API base path | `https://flowdesk-backend.fly.dev/api/v1` |
| Health probe | `https://flowdesk-backend.fly.dev/health` → `{"status":"ok"}` |
| Interactive docs | `https://flowdesk-backend.fly.dev/docs` · `/redoc` · `/openapi.json` |
| Supabase project | `https://tqkyghashkawgqbnrwaf.supabase.co` |
| Frontend | `https://flowdesk.vanelsen.net.au` |
| Region | Fly.io `syd` (Sydney) |
| Reporting timezone | `Australia/Melbourne` (drives analytics week buckets — §4.9) |

`/docs`, `/redoc` and `/openapi.json` are **public and not gated by environment.** There is a
`APP_ENV` setting but nothing in the code reads it, so the interactive docs are reachable in
production by anyone. Flagged as a defect in §7.2.

### 1.4 Endpoint index

26 operations across 19 paths. Everything under `/api/v1` except `GET /health`.

| Method | Path | Roles | Success | § |
|---|---|---|---|---|
| GET | `/health` | **public** | `200` | 4.1 |
| GET | `/me` | any authenticated | `200` | 4.1 |
| POST | `/organizations` | **public** | `201` | 4.2 |
| GET | `/categories` | any authenticated | `200` | 4.3 |
| POST | `/categories` | `tenant_admin` | `201` | 4.3 |
| GET | `/categories/{category_id}` | any authenticated | `200` | 4.3 |
| PATCH | `/categories/{category_id}` | `tenant_admin` | `200` | 4.3 |
| DELETE | `/categories/{category_id}` | `tenant_admin` | `204` | 4.3 |
| GET | `/users` | `tenant_admin`, `system_admin` | `200` | 4.4 |
| POST | `/users` | `tenant_admin`, `system_admin` | `201` | 4.4 |
| GET | `/users/{user_id}` | `tenant_admin`, `system_admin` | `200` | 4.4 |
| PATCH | `/users/{user_id}` | `tenant_admin`, `system_admin` | `200` | 4.4 |
| POST | `/users/{user_id}/deactivate` | `tenant_admin`, `system_admin` | `200` | 4.4 |
| POST | `/users/{user_id}/activate` | `tenant_admin`, `system_admin` | `200` | 4.4 |
| POST | `/incidents` | `staff` | `201` | 4.5 |
| GET | `/incidents` | any authenticated | `200` | 4.5 |
| GET | `/incidents/{incident_id}` | any authenticated | `200` | 4.5 |
| GET | `/incidents/{incident_id}/transitions` | any authenticated | `200` | 4.5 |
| POST | `/incidents/{incident_id}/transitions` | `reviewer`, `tenant_admin` | `200` | 4.6 |
| POST | `/incidents/{incident_id}/assign` | `tenant_admin` | `200` | 4.6 |
| GET | `/notifications` | any authenticated | `200` | 4.8 |
| GET | `/notifications/unread-count` | any authenticated | `200` | 4.8 |
| POST | `/notifications/{notification_id}/read` | any authenticated | `200` | 4.8 |
| POST | `/notifications/read-all` | any authenticated | `200` | 4.8 |
| GET | `/analytics/volume` | `tenant_admin`, `system_admin` | `200` | 4.9 |
| GET | `/analytics/status-distribution` | `tenant_admin`, `system_admin` | `200` | 4.9 |

"Any authenticated" means there is no role gate — the *row set* is narrowed by role instead of
the call being refused. That distinction matters: those endpoints can never return
`403 insufficient_role`.

§4.7 was "Reserved (Sprint 3)". Those stubs are deleted; the section number is retired rather
than reused, so existing references to §4.8 / §4.9 stay valid.

---

## 2. Authentication & Authorization

**The single most important thing for the frontend: FlowDesk has no login endpoint.** The
frontend authenticates against Supabase, receives a JWT, and sends it to the FlowDesk API,
which only verifies it. There is no login, logout, refresh, signup or password endpoint on this
API. `GET /me` is the only identity endpoint.

### 2.1 Model

Authentication is delegated to Supabase; authorization is enforced by the backend.

- The frontend logs the user in against Supabase directly and receives a signed JWT. The
  backend never receives or stores credentials.
- The frontend attaches that JWT as a Bearer token on every FlowDesk request. The backend
  verifies it asymmetrically against Supabase's JWKS endpoint on every request — no
  server-side session, no token exchange.
- **Role and tenant are NOT read from the token.** The backend loads them from its own database
  on every single request. A role change, or a deactivation, therefore takes effect on the
  user's very next request even though their JWT is still cryptographically valid. To learn the
  current user's role and tenant, call `GET /me`.

### 2.2 Supabase client configuration

Initialise `supabase-js` with the project URL and the publishable (anon) key. The anon key is
designed to be embedded in the browser — it is public and safe to commit to the frontend. The
`service_role` / secret key must **never** appear in frontend code; it is backend-only and is
held as a Fly secret.

| Setting | Value |
|---|---|
| `SUPABASE_URL` | `https://tqkyghashkawgqbnrwaf.supabase.co` |
| Publishable (anon) key | `sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24` |
| Access token algorithms accepted | `RS256`, `ES256` only — verified via JWKS |
| Access token lifetime | 3600 s (1 hour) — NFR-11 |

`HS256` is deliberately rejected. A token signed with a symmetric key is refused even if the
key is correct, because accepting `HS256` alongside JWKS is the classic algorithm-confusion
vulnerability.

### 2.3 Login flow

```js
import { createClient } from '@supabase/supabase-js'

const supabase = createClient(
  'https://tqkyghashkawgqbnrwaf.supabase.co',
  'sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24'   // publishable key — safe in the browser
)

// 1) Authenticate against Supabase (the backend never sees the password)
const { data, error } = await supabase.auth.signInWithPassword({ email, password })
if (error) throw error
const token = data.session.access_token

// 2) Resolve identity / role / tenant from the FlowDesk backend
const res = await fetch('https://flowdesk-backend.fly.dev/api/v1/me', {
  headers: { Authorization: `Bearer ${token}` },
})
const me = await res.json()   // { id, email, name, role, status, tenant }
```

Use `me.role` to drive routing and UI, and send the same Bearer token on every subsequent
request.

### 2.4 Using supabase-js

```bash
npm install @supabase/supabase-js
```

```js
// lib/supabase.js — one shared client for the whole app
import { createClient } from '@supabase/supabase-js'

export const supabase = createClient(
  'https://tqkyghashkawgqbnrwaf.supabase.co',
  'sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24',
  { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } }
)
```

| `supabase-js` call | Purpose |
|---|---|
| `auth.signInWithPassword({email, password})` | Log in; returns a session with `access_token` + `refresh_token` |
| `auth.getSession()` | Read the current session / token right before an API call |
| `auth.onAuthStateChange((e, s) => …)` | React to login / logout / token refresh app-wide |
| `auth.refreshSession()` | Force a token refresh (usually automatic) |
| `auth.updateUser({password})` | Set / change the password (used in the invite flow, §2.6) |
| `auth.signOut()` | Log out and clear the stored session |
| `auth.resetPasswordForEmail(email)` | Send a password-reset email |

A reusable helper that attaches the token to every FlowDesk request:

```js
// lib/api.js
import { supabase } from './supabase'

const BASE = 'https://flowdesk-backend.fly.dev/api/v1'

export async function api(path, init = {}) {
  const { data: { session } } = await supabase.auth.getSession()
  const res = await fetch(BASE + path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init.headers || {}),
      Authorization: `Bearer ${session?.access_token ?? ''}`,
    },
  })
  return res
}
```

See §3.1.6 for what to do with a `401` — the refresh-and-retry decision depends on
`details.reason`, not on the status alone.

### 2.5 Getting a token with curl (for testing)

```bash
# 1) Log in at Supabase -> access_token
TOKEN=$(curl -s "https://tqkyghashkawgqbnrwaf.supabase.co/auth/v1/token?grant_type=password" \
  -H "apikey: sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"your-password"}' | jq -r .access_token)

# 2) Call the FlowDesk API with it
curl https://flowdesk-backend.fly.dev/api/v1/me -H "Authorization: Bearer $TOKEN"
```

The user must already have a password — set via the invite email (§2.6), or created directly in
the Supabase Dashboard (Authentication → Users → Add user, with **Auto Confirm User** enabled).
The dashboard route is the practical way to stand up test accounts without waiting on email.

### 2.6 First-time password — the invite flow

**New accounts are created without a password, and no password or token is ever returned by
this API.** Both `POST /organizations` and `POST /users` create the Supabase auth user
server-side, and Supabase emails the person a secure set-password link.

The frontend must therefore handle the Supabase invite/recovery redirect. When the user arrives
from the email link, `supabase-js` parses the URL fragment and establishes a temporary session;
the app then calls `updateUser` to set the password:

```js
// User lands on the app from the Supabase email link (type=invite / recovery).
const { error } = await supabase.auth.updateUser({ password: newPassword })
// On success the user is fully signed in and can call the FlowDesk API.
```

The redirect target is configured backend-side as `INVITE_REDIRECT_URL`, currently
`https://flowdesk.vanelsen.net.au/set-password`. **That page must exist for any invited user to
ever log in** — it is the load-bearing half of the flow and the backend cannot substitute for
it.

### 2.7 Token lifetime & refresh

Access tokens expire after 1 hour. `supabase-js` refreshes them automatically using the stored
refresh token.

Verification uses **zero clock leeway** — a token one second past `exp` is already rejected. Do
not assume a grace period.

Defensive handling: if a FlowDesk request returns `401` with
`details.reason === "token_expired"`, refresh the session and retry once; if the refresh fails,
redirect to login. See §3.1.6 for why you must branch on `reason` rather than on `401`.

### 2.8 Sending the token

```
Authorization: Bearer <supabase_access_token>
```

Required on every endpoint except the public ones: `GET /health`, `POST /api/v1/organizations`,
and the docs routes (`/docs`, `/redoc`, `/openapi.json`).

Two things that surprise people:

- **`401` responses carry no `WWW-Authenticate` header.** The bearer scheme is configured with
  `auto_error=False` so that failures render in the shared envelope instead of FastAPI's
  default. The consequence is that the envelope is the *only* machine signal — there is no
  standard auth-challenge header to key on.
- **A non-Bearer scheme is `missing_token`, not `invalid_token`.** `Authorization: Basic abc`,
  and a raw JWT sent with no scheme at all, both produce `missing_token`, because the bearer
  parser returns nothing at all rather than a bad credential.

### 2.9 Roles and capability matrix

Four roles exist. This is the full shipped surface, not a per-sprint subset.

| Capability | `system_admin` | `tenant_admin` | `staff` | `reviewer` |
|---|---|---|---|---|
| `GET /me` | ✅ | ✅ | ✅ | ✅ |
| List / read users | ✅ any tenant | ✅ own tenant | ❌ 403 | ❌ 403 |
| Invite user | ✅ any tenant | ✅ own tenant | ❌ 403 | ❌ 403 |
| Invite / grant `system_admin` | ✅ | ❌ 403 `privilege_escalation` | ❌ 403 | ❌ 403 |
| Patch / (de)activate user | ✅ any tenant | ✅ own tenant | ❌ 403 | ❌ 403 |
| Read categories | ✅ *own tenant only* | ✅ own tenant | ✅ own tenant | ✅ own tenant |
| Create / patch / delete category | ❌ **403** | ✅ | ❌ 403 | ❌ 403 |
| Submit incident | ❌ 403 | ❌ 403 | ✅ | ❌ 403 |
| List / read incidents | ✅ all tenants | ✅ own tenant | own submissions | assigned + unassigned |
| Transition incident | ❌ **403** | ✅ | ❌ 403 | ✅ |
| Reassign incident | ❌ **403** | ✅ | ❌ 403 | ❌ 403 |
| Notifications (own only) | ✅ | ✅ | ✅ | ✅ |
| Analytics | ✅ all tenants | ✅ own tenant | ❌ 403 | ❌ 403 |

**Three asymmetries worth building around, because they are easy to get wrong:**

1. **`system_admin` is *excluded* from category writes and from all workflow actions.** It is
   not a superset of `tenant_admin`. `POST/PATCH/DELETE /categories`,
   `POST /incidents/{id}/transitions` and `POST /incidents/{id}/assign` all return
   `403 insufficient_role` for a System Admin. Whether that is intended is an open question
   (§7.2), but it is what ships.
2. **`system_admin` reading categories sees only its own tenant's.** The category routes never
   consult the cross-tenant scope object; they filter on the caller's own `tenant_id`. So a
   System Admin has cross-tenant reach for users, incidents and analytics, but *not* for
   categories.
3. **`staff` is the only role that can submit an incident.** A Tenant Admin cannot file one on a
   user's behalf.

Role changes and deactivations take effect on the user's next request (§2.1).

### 2.10 Tenant isolation

Every tenant-scoped resource is filtered to the caller's tenant. Attempting to reach a resource
belonging to another tenant returns **`404 Not Found`, never `403`**, so the API never reveals
whether a foreign resource exists (NFR-12).

A `system_admin` bypasses the tenant filter and may target any tenant, optionally pinning one
with `?tenant_id=`. For any other role `?tenant_id=` is **silently ignored, not rejected** — you
will get your own tenant's data back with a `200`, not an error.

**One artefact to be aware of:** where a role gate exists, it runs *before* the handler, so a
`staff` user probing another tenant's incident id on `POST /incidents/{id}/transitions` receives
`403 insufficient_role`, not `404`. That is not an existence leak — the `403` is a pure function
of the caller's role and is byte-identical for real and invented ids. Past the role gate,
out-of-scope is always `404`.

### 2.11 What the backend actually verifies about a token

Useful when debugging a `401`. The backend requires all of:

- a signature valid against Supabase's JWKS, using `RS256` or `ES256`;
- the `exp`, `sub`, `aud` and `iss` claims to be **present** (all four are required);
- `aud` and `iss` to match the configured expectations;
- `exp` to be in the future, with zero leeway;
- and then, separately, a row in the FlowDesk `users` table whose primary key equals `sub`,
  with `status = active`.

The last point is the one that catches people out during setup: a Supabase auth user created in
the dashboard but never invited through FlowDesk has a perfectly valid token and still gets
`401 user_not_provisioned` on every call.

---

## 3. Conventions

### 3.1 Error envelope

#### 3.1.1 The shape

Every error the application raises uses one shape, so the frontend can rely on a single parser:

```json
{
  "error": {
    "code": "<machine_slug>",
    "message": "<human-readable text>",
    "details": { }
  }
}
```

`error.code` is coarse (one per HTTP status). `error.details.reason` is the fine-grained,
machine-readable discriminator, and it is what you should branch on.

#### 3.1.2 Status → `error.code`

| HTTP | `error.code` | Envelope? |
|---|---|---|
| 401 | `unauthorized` | ✅ |
| 403 | `forbidden` | ✅ |
| 404 | `not_found` | ✅ |
| 405 | `method_not_allowed` | ✅ (but see §3.1.4) |
| 409 | `conflict` | ✅ |
| 422 | `validation_error` | ✅ |
| 502 | `upstream_error` | ✅ — Supabase Auth refused the call |
| 503 | `upstream_unavailable` | ✅ — Supabase Auth is transiently unavailable; may carry `Retry-After` |
| any other 4xx from the router | `http_error` | ✅ — fallback, message is the HTTP phrase |
| 400 | — | ❌ CORS rejection, plain text |
| 500 | — | ❌ plain text |

**502/503 are new in Sprint 4** and are the only enveloped 5xx. They replace what used to be a
plain-text `500` on every Supabase Admin failure (D-1, §7.2). A `>= 500` branch that skips
envelope parsing — like the snippet in §3.1.4 — will now discard a message the user could act on:
treat `502`/`503` as enveloped and keep the plain-text fallback for `500` only.

`error.code` is `http_error` for any routing-level status outside the five mapped above. It is
not reachable through normal use today, but a client's `switch` on `error.code` should have a
default arm rather than assuming the list is closed.

#### 3.1.3 Complete reason register

This is every error the application can produce. `details.reason` is listed as `—` where the
raise site passes no details, in which case `details` is the empty object `{}` and the only
discriminator is the message string. Those gaps are tracked as a defect in §7.2.

**401 `unauthorized`**

| `details.reason` | Message | Trigger |
|---|---|---|
| `missing_token` | `Missing authentication token.` | No `Authorization` header; empty header; `Bearer` with nothing after it; a non-Bearer scheme; a scheme-less raw token |
| `token_expired` | `Token has expired.` | `exp` at or before now, zero leeway. **This is the slug to key silent refresh-and-retry on.** |
| `invalid_token` | `Invalid authentication token.` | Bad or tampered signature; wrong `aud`; wrong `iss`; a missing required claim; an algorithm outside `RS256`/`ES256`; **and every JWKS failure** (see the warning below) |
| `verification_failed` | `Unable to verify authentication token.` | Effectively unreachable — see the warning below |
| `user_not_provisioned` | `User is not provisioned.` | The token verified but `sub` matches no row in `users`, or `sub` is not a parseable UUID |
| `—` | `User tenant not found.` | `GET /me` only: the user's row references a `tenant_id` with no matching tenant. **The only 401 in the API with no reason slug.** |

> **Correction to the v1.0 contract — read this if you wrote auth error handling.**
> v1.0 documented `verification_failed` as the slug for a JWKS outage ("Supabase JWKS endpoint
> unreachable, unknown `kid`"), and advised treating it differently from `invalid_token`. **That
> is wrong.** Every JWKS failure — endpoint unreachable, timeout, HTTP 5xx, unknown `kid` —
> raises an exception type that is caught one branch earlier and rendered as
> **`invalid_token`**. `verification_failed` is reachable only for a non-JWT exception, in
> practice only a misconfigured JWKS URL at boot.
>
> **Consequence you must handle:** during a Supabase JWKS incident, a perfectly good token looks
> byte-identical to a forged one. A client that force-logs-out on `invalid_token` will log every
> user out. Prefer logging out on `token_expired`-then-failed-refresh, and treating
> `invalid_token` as retryable at least once before destroying the session.

**403 `forbidden`**

| `details.reason` | Message | Trigger |
|---|---|---|
| `account_deactivated` | `Your account has been deactivated. Contact your administrator.` | The user row exists and the token is valid but `status = inactive`. Checked on **every** request, before any role check, so it fires on read-only calls too. |
| `insufficient_role` | `You do not have permission to perform this action.` | Role not in the gate's allow-list. **Also carries `details.required`** — a JSON array of the exact role values accepted, e.g. `["tenant_admin", "system_admin"]`. Render your message from that array rather than hardcoding it. |
| `privilege_escalation` | `Only System Admins can create System Admin accounts.` | `POST /users` with `role: "system_admin"` from a non-System-Admin |
| `privilege_escalation` | `Only System Admins can grant the System Admin role.` | `PATCH /users/{id}` setting `role: "system_admin"` from a non-System-Admin |

Note the two `privilege_escalation` cases share a slug but have different messages — you cannot
tell create from grant using `details` alone, only by which endpoint you called.

**404 `not_found`** — all carry `details: {}` except `notification_not_found`.

| `details.reason` | Message | Trigger |
|---|---|---|
| `—` | `User not found.` | `/users/{id}` — no such user, **or** another tenant's user |
| `—` | `Category not found.` | `/categories/{id}` — no such category, **or** another tenant's |
| `—` | `Incident not found or you do not have permission to view it.` | `/incidents/{id}` and its sub-paths — absent, another tenant's, or outside your visibility |
| `—` | `Incident not found.` | `POST /incidents` internal re-read. Not reachable in normal operation, but it is a **distinct string**, so message-matching code must handle both |
| `notification_not_found` | `Notification not found.` | `POST /notifications/{id}/read` — absent, someone else's, or another tenant's |
| `—` | `Not Found` | **Starlette's**, for a URL matching no route at all. Same `code` and `details: {}` as a real scope 404, distinguishable only by the message |

**409 `conflict`** — every one carries `details: {}` **except** the two workflow slugs.

| `details.reason` | Message | Trigger |
|---|---|---|
| `—` | `An organisation with this name already exists.` | `POST /organizations`: case-insensitive pre-check, **and** a database integrity failure on commit. Three different constraints funnel into this one message |
| `—` | `A user with this email is already registered.` | `POST /organizations`: the email exists in Supabase Auth globally |
| `—` | `A user with this email already exists in your organisation.` | `POST /users`: two distinct raise sites emit this identical string |
| `—` | `A user with this email already exists.` | `POST /users`: registered globally in Supabase and recovery failed |
| `—` | `A category with this name already exists.` | `POST`/`PATCH /categories`: unique per `(tenant, name)`, compared **exactly** — case- and whitespace-sensitive |
| `—` | `This category is used by open incidents and cannot be deleted.` | `DELETE /categories/{id}` with at least one referencing incident that is **not closed** |
| `invalid_transition` | `This transition is not permitted from the current state.` | §4.6. **Also carries `from_status`, `to_status`, `allowed`** |
| `incident_closed` | `A closed incident cannot be reassigned.` | `POST /incidents/{id}/assign` on a closed incident |

**422 `validation_error`** — two different animals sharing one code, see §3.1.5.

| `details.reason` | Message | Trigger |
|---|---|---|
| `—` (has `details.errors`) | `Request validation failed.` | Framework validation: missing/mistyped field, length violation, bad email, non-UUID path param, out-of-range `limit`/`offset`, value outside an enum |
| `category_not_in_tenant` | `The selected category does not exist in your organisation.` | `POST /incidents` |
| `resolution_note_required` | `A resolution note is required to close this incident.` | Closing without a note |
| `invalid_assignee` | `The selected user is not an active reviewer in this organisation.` | `POST /incidents/{id}/assign` |
| `invalid_date_range` | `The start date must not be after the end date.` | `GET /analytics/volume` |
| `date_range_too_large` | `The requested date range is too large. Request at most 53 weeks.` | `GET /analytics/volume`. **Also carries `max_weeks`** |

**502 `upstream_error` / 503 `upstream_unavailable`** — Supabase Auth failed.

| HTTP | `details.reason` | Message | Trigger |
|---|---|---|---|
| 503 | `identity_provider_unavailable` | `The identity provider is temporarily unavailable. Please try again shortly.` | GoTrue answered `429` (notably `over_email_send_rate_limit`, which the built-in SMTP returns after a couple of invites) or `5xx`, or the call timed out |
| 502 | `identity_provider_error` | `The identity provider rejected the request.` | Any other `4xx` from GoTrue — a bad service-role key, a wrong project URL |

Both carry `details.operation` (`invite_user`, `get_user_by_email`, `delete_user`) for the log
correlation; do not branch on it. Reachable from `POST /organizations` and `POST /users` only —
they are the two endpoints that call Supabase Auth.

**Retry semantics.** `503` is the retryable one and echoes GoTrue's `Retry-After` header when it
sends one; absent that header, back off yourself rather than assuming a delay. `502` will not fix
itself — surface it and stop. The upstream response body is never included: it is logged
server-side, because `POST /organizations` is public.

**405 `method_not_allowed`** — message `Method Not Allowed`, `details: {}`.

Produced when the path exists but the method does not, e.g. `POST /api/v1/notifications`,
`POST /api/v1/analytics/volume`, `DELETE /api/v1/users/{id}`, `PUT /api/v1/categories/{id}`,
`POST /health`.

**Note:** the `Allow` response header is **dropped** by the error handler. HTTP says a 405 should
list the permitted methods; this API does not. Do not build anything that reads `Allow`.

#### 3.1.4 Responses that are NOT in the envelope

**This is the most important correction in this document.** The envelope is not universal, and a
client that assumes it is will throw a JSON parse error at the worst possible moment.

| Status | Content-Type | Body | Cause |
|---|---|---|---|
| **500** | `text/plain` | `Internal Server Error` | Any unhandled server-side exception — **including a missing `Content-Type`, see below** |
| **400** | `text/plain` | `Disallowed CORS origin` | CORS preflight from an origin not in the allow-list |
| **307** | — | empty, `Location` header only | A trailing slash, e.g. `GET /api/v1/users/`. Not enveloped and **not authenticated** |
| **405** | ✅ enveloped | `Method Not Allowed` | Raised by the router **before** auth — so it pre-empts `401`, see §3.1.6 |

Only three exception handlers are registered — for application errors, request validation, and
routing errors. **Nothing catches a generic exception**, so any unexpected failure is answered
by the server's default plain-text 500 and never reaches the envelope.

> **On the sign-up form, always send `Content-Type: application/json`.**
>
> `POST /api/v1/organizations` with a non-empty body and any other content type — `text/plain`,
> `application/x-www-form-urlencoded`, or **no `Content-Type` header at all** — returns
> **`500 Internal Server Error` as plain text**, not a `422` and not a `415`.
>
> Verified against production on 30 July 2026: all three variants return
> `500` / `text/plain` / `Internal Server Error`.
>
> The cause is a bug in the validation handler, not in your request: the framework leaves the
> body as raw bytes, the validation error carries those bytes, and serialising them to JSON
> throws inside the error handler itself. The response also carries **no
> `access-control-allow-origin` header**, so in a browser it surfaces as an opaque CORS failure
> rather than as a 500 — which makes it maximally confusing to debug from the frontend.
>
> **Scope:** only `POST /api/v1/organizations` is affected, because it is the only public
> endpoint that takes a body. On every authenticated endpoint the `401`/`403` fires *before* the
> body is decoded, so a logged-in user cannot trigger it. That still makes it the registration
> screen — the first thing a new user ever touches, and the one most likely to be hand-rolled
> outside the shared `api()` helper. Tracked as D-13 in §7.2.

A `NaN` or `Infinity` literal in the body of that same endpoint triggers the same handler crash,
for the same reason — a `500` instead of a `422` (D-14, also verified on production).

By contrast, **malformed JSON *with* the right `Content-Type` behaves correctly**: a proper `422`
envelope, verified live as
`{"error":{"code":"validation_error","message":"Request validation failed.","details":{"errors":[{"type":"json_invalid","loc":["body",5],…}]}}}`.
Note the integer in `loc` — see §3.1.5.

**Supabase Admin failures are no longer among these.** They used to be the most reachable 500 in
the API — a bad service-role key, a wrong project URL, the GoTrue rate limit, unconfigured SMTP
or a network timeout during `POST /organizations` or `POST /users` all produced a plain-text
`500`. They are now enveloped `502`/`503` with a reason slug (§3.1.3). Fixed in Sprint 4; see
D-1 in §7.2.

The one that remains: `DELETE /categories/{id}` for a category referenced only by **closed**
incidents (§4.3, D-2). It is a real defect and until it is fixed the frontend must survive it.

**What to do:**

```js
// Guard envelope parsing. Never assume a body is JSON.
async function parseError(res) {
  // 502/503 ARE enveloped and carry a message worth showing — only 500 is plain text.
  if (res.status >= 500 && res.status !== 502 && res.status !== 503) {
    return { code: 'internal_error', message: 'Server error. Try again.' }
  }
  try {
    const body = await res.json()
    return body.error ?? { code: 'unknown', message: 'Unexpected response' }
  } catch {
    return { code: 'unknown', message: await res.text().catch(() => 'Unexpected response') }
  }
}
```

Also build URLs **without** trailing slashes — `/api/v1/users`, not `/api/v1/users/` — or you
will collect a redirect on every list call, which `fetch` follows but which turns a `POST` into
a mess. The redirect is issued by the router before authentication, so it is neither enveloped
nor gated.

**And the redirect downgrades the scheme.** Verified live: `GET https://…/api/v1/me/` answers
`307` with `Location: http://flowdesk-backend.fly.dev/api/v1/me` — plain `http`, because the app
rebuilds an absolute URL without honouring the proxy's forwarded-proto header. A browser on an
HTTPS page will refuse to follow that as mixed content, so a stray trailing slash fails in the
browser while working fine in curl. Tracked as D-15.

One more routing quirk: **`HEAD /health` returns `405`**, because a `GET` route here does not
implicitly accept `HEAD`. If your uptime monitor probes with `HEAD`, point it at `GET`.

`501 not_implemented` appears in the v1.0 contract. It is now **dead** — the reserved stubs were
deleted in Sprint 3 and nothing raises it. Remove any 501 handling you still have.

#### 3.1.5 Two kinds of 422

`validation_error` covers both framework validation and business rules, and the HTTP status and
`error.code` are identical. The discriminator is which key is present in `details`:

| | `details` contains | Meaning |
|---|---|---|
| Framework | `errors: [...]` (pydantic list, each with `type`, `loc`, `msg`, and sometimes `input`/`ctx`) | The request was malformed. Map `loc` to your form fields. |
| Business rule | `reason: "<slug>"` | The request was well-formed but the rule said no. Show the message. |

So: `if (details.reason) { … } else if (details.errors) { … }`. Never assume `reason` exists on a
422.

**`loc` is not always `["body", "<field>"]`.** Do not read `loc[1]` as a field name
unconditionally:

| Case | `loc` | `type` |
|---|---|---|
| A field failed | `["body", "title"]` | `missing`, `string_too_short`, `string_too_long`, `value_error`, `enum`, … |
| A query/path param failed | `["query", "limit"]` / `["path", "user_id"]` | `less_than_equal`, `greater_than_equal`, `uuid_parsing`, `int_parsing`, … |
| **Malformed JSON** | `["body", 14]` — an **integer character offset** | `json_invalid` |
| **Body absent entirely** | `["body"]` — a **single element** | `missing` |

A form-error mapper written as `errors[i].loc[1]` will render `"14"` as a field name, or crash
on `undefined`. Guard on `typeof loc[1] === 'string'` and fall back to a form-level message.

`ctx` carries the constraint when there is one — `{"le": 100}` for `limit`, or
`{"expected": "'open', 'in_review' or 'closed'"}` for an enum — which is worth surfacing
verbatim rather than re-writing the bound in the client.

#### 3.1.6 Precedence — which error wins

For a single request, checks fire in this order. This matters because the *first* failure is the
only one you see.

1. **CORS** — a disallowed origin fails the preflight with a plain-text `400` before anything
   else runs.
2. **`307`** — a trailing slash redirects before anything is checked.
3. **`404` / `405` from the router** — an unmatched path or an unregistered method.
4. **`401`** — token problems.
5. **`403 account_deactivated`**.
6. **`403 insufficient_role`**.
7. **`422`** — path, query and body validation.
8. **`404`** — out-of-scope or absent resource.
9. **`403 privilege_escalation`**.
10. **`409`** — conflicts.

**Note steps 3 and 4: the method check beats authentication.** An *unauthenticated*
`POST /api/v1/notifications` returns `405 method_not_allowed`, not `401 missing_token`, because
the router rejects the method before any dependency runs. So a `405` tells you nothing about
whether your token was valid.

**Note steps 6 and 7: the role gate beats validation.** Role checks run as dependencies, which
the framework resolves *before* validating the endpoint's own parameters. So
`GET /api/v1/users?limit=0` with a `staff` token returns `403 insufficient_role`, not `422`, and
`PATCH /api/v1/users/not-a-uuid` from a `reviewer` returns `403`, not `422`. If you are
debugging "why am I not getting the validation error I expect", this is why.

**The body is decoded last.** Authentication and the role gate both run before the request body
is looked at, so on an authenticated endpoint a malformed body, a bad `Content-Type` and an
invalid field are all invisible until the caller is past steps 4–6. Verified live: an
unauthenticated `POST /api/v1/incidents` carrying a `text/plain` body returns
`401 missing_token`, not the `500` from §3.1.4. That is also why the `Content-Type` defect
(D-13) is confined to the one public endpoint that accepts a body.

`OPTIONS` never reaches any of this. CORS preflight is answered `200` by middleware for every
path, including method/path combinations that would otherwise be `405`, and without consulting
your token.

### 3.2 Pagination

List endpoints accept `limit` and `offset` and return a `data` + `pagination` envelope.

| Query | Rule | Default |
|---|---|---|
| `limit` | integer, 1–100 | 50 |
| `offset` | integer, ≥ 0 | 0 |

```json
{
  "data": [ /* items */ ],
  "pagination": { "limit": 50, "offset": 0, "total": 1 }
}
```

`total` is the count matching the filter *before* `limit`/`offset`, so it is what you paginate
against.

**Note `limit` has a minimum of 1** — you cannot request a count-only page with `limit=0`; that
is a `422`.

**Three endpoints deliberately do not use this envelope:**

- `GET /incidents/{id}/transitions` returns a **bare JSON array**, unpaginated (§4.5).
- `GET /analytics/volume` returns a `data` key with window metadata instead of `pagination`
  (§4.9).
- `GET /analytics/status-distribution` returns `data` + `total` (§4.9).

### 3.3 Data types and enums

- IDs are UUID strings.
- Timestamps (`created_at`, `updated_at`) are ISO-8601 UTC strings.
- Dates (`week_start`, and the `from`/`to` analytics params) are plain `YYYY-MM-DD`.
- Enums are lowercase strings:

| Enum | Values | Notes |
|---|---|---|
| `role` | `system_admin`, `tenant_admin`, `staff`, `reviewer` | |
| `status` (user) | `active`, `inactive` | |
| `severity` | `low`, `medium`, `high`, `critical` | this order is also the sort order |
| `incident_status` | `open`, `in_review`, `closed` | likewise; also the display order of the analytics buckets |
| `sort` (incidents) | `created_at`, `updated_at`, `title`, `severity`, `status` | |
| `order` | `asc`, `desc` | |

An unknown enum value is a framework `422` with `details.errors`, not a domain error — which is
why these are enums rather than free strings: bad values are rejected natively and there is no
extra slug to branch on.

**Only incidents have an `updated_at`.** The `users` and `categories` tables have no such column
at all, so there is no way to tell when a user or category was last modified, and no hidden
field waiting to be exposed later.

### 3.4 Idempotency

Two endpoints are idempotent, and both are idempotent **in effect while remaining `POST` in
method**. The governing principle:

> Idempotency is defined over the postcondition, not over the request.

- `POST /incidents/{id}/transitions` — asking for a state the incident already occupies returns
  `200` with the current state and writes nothing (§4.6).
- `POST /notifications/{id}/read` — a second call returns a byte-identical body and writes
  nothing (§4.8).

`POST /notifications/read-all` is also safe to repeat; the second call reports
`marked_read: 0`.

So a double-click, a retry after a dropped connection, or a duplicate submission from a flaky
network are all safe on those routes. They are **not** safe on `POST /incidents`,
`POST /users`, `POST /organizations` or `POST /categories`, none of which deduplicate — a retry
there either creates a second row or returns a `409`.

---

## 4. Endpoints

Every endpoint below can additionally return any of the auth errors from §3.1.3 (`401` family,
`403 account_deactivated`), the framework `422`, and a non-enveloped `500`. Those are not
repeated per endpoint.

### 4.1 Session

#### `GET /api/v1/me`

Any authenticated user. `200` → `MeResponse`.

```json
{
  "id": "3f1c2b7a-…",
  "email": "admin@acme.com",
  "name": "Ada Lovelace",
  "role": "tenant_admin",
  "status": "active",
  "tenant": { "id": "9a8b…", "name": "Acme Pty Ltd" }
}
```

Call this immediately after login: it is the only way to learn the caller's role and tenant,
because neither is readable from the token (§2.1). `id` equals the Supabase auth user id.

Errors: `401` `—` `User tenant not found.` if the user's tenant row is missing (§3.1.3).

#### `GET /health`

**Public.** `200` → `{"status": "ok"}`.

Registered on the bare app, **not** under `/api/v1`. No version, no build info, and **no
database check** — a `200` here means the process is up, not that the API is healthy. Used by
Fly.io health checks.

### 4.2 Organizations

#### `POST /api/v1/organizations`

**Public — no token.** This is the self-service sign-up path and the only way to create a
tenant. `201` → `OrgRegisterResponse`.

```jsonc
// request
{
  "organization_name": "Acme Pty Ltd",   // required, 2..255
  "admin_email": "admin@acme.com",       // required, valid email
  "admin_name": "Ada Lovelace"           // required, 1..255
}
```

```jsonc
// 201 response
{
  "tenant":     { "id": "uuid", "name": "Acme Pty Ltd", "created_at": "2026-07-30T…Z" },
  "admin_user": { "id": "uuid", "email": "admin@acme.com", "name": "Ada Lovelace",
                  "role": "tenant_admin", "status": "active" }
}
```

It creates the tenant and its first `tenant_admin` in one transaction, and creates the matching
Supabase auth user. **No password and no token are returned** — Supabase emails the admin a
set-password link (§2.6). The admin cannot log in until they follow it.

**Errors**

| Status | `reason` | Message | Cause |
|---|---|---|---|
| 409 | `—` | `An organisation with this name already exists.` | Name taken, compared **case-insensitively**. Also emitted when the database rejects the insert — see the note below |
| 409 | `—` | `A user with this email is already registered.` | The email already exists in Supabase Auth, globally across all tenants |
| 422 | `—` | `Request validation failed.` | Field constraints |
| 405 | `—` | `Method Not Allowed` | Any method other than POST — there is no list, get or update for organisations |

**Things to know before you build the sign-up form:**

1. **Organisation names are unique case-insensitively**, so `Acme` collides with `ACME`. User
   emails are unique per tenant case-insensitively; category names are unique per tenant
   **case-sensitively** (§4.3). Three subtly different rules — do not generalise from one.
2. **`admin_email` is normalised before storage, so the echoed value can differ from what you
   sent.** Surrounding whitespace is stripped, a `Name <addr@example.com>` form is reduced to
   the bare address, and the domain is lower-cased. Read `admin_user.email` from the response
   rather than assuming it matches your input. `organization_name` and `admin_name`, by
   contrast, are stored **completely raw** — no trimming, no case folding — so `"  Acme  "`
   is stored with its spaces.
3. **The 409 on organisation name has two indistinguishable causes.** Besides the obvious
   name-taken case, a database integrity failure on commit also renders as this message — which
   includes a concurrent duplicate registration losing the race, and an unrelated user-row
   collision. A `409` mentioning the organisation name does not strictly prove the name is the
   problem.
4. **Failures after the Supabase user is created are only partly compensated.** If the database
   insert fails, the backend deletes the auth user it just created, so the email is free to
   retry. But if the Supabase call itself **times out**, the auth user may well have been
   created (and the invite email sent) with no response to act on — nothing is rolled back, and
   that email is then permanently stuck on `A user with this email is already registered.`
   Recovery is manual, via the Supabase dashboard. Tracked in §7.2.
5. **Unknown body fields are silently ignored**, not rejected.

### 4.3 Categories

Categories are per-tenant labels for incidents. Read by everyone, written only by
`tenant_admin`.

#### `GET /api/v1/categories`

Any authenticated user. `200` → `Page[CategoryOut]`.

Scope is the caller's **own** tenant, always — including for a `system_admin`, who gets no
cross-tenant reach here and has no `?tenant_id=` parameter available. Query params are `limit`
and `offset` only; there is no name filter or search.

Ordered by `name` ascending. The exact collation is not pinned by the application, so treat the
position of mixed-case names as unspecified rather than assuming uppercase-first.

#### `POST /api/v1/categories`

**`tenant_admin` only** — a `system_admin` gets `403 insufficient_role` with
`required: ["tenant_admin"]`. `201` → `CategoryOut`.

```jsonc
{ "name": "Hardware", "description": "Physical equipment faults" }   // name 1..255, description ≤2000 nullable
```

`tenant_id` is forced from the caller and cannot be set from the body — a `tenant_id` key in the
body is **silently discarded**, not rejected.

`409 A category with this name already exists.` on a duplicate. Uniqueness is per tenant and
**exact**: case- and whitespace-sensitive, so `Hardware`, `hardware` and `Hardware ` can all
coexist. The conflicting name is not echoed back.

#### `GET /api/v1/categories/{category_id}`

Any authenticated user. `200` → `CategoryOut`. `404 Category not found.` for absent or
another tenant's. A malformed UUID is `422`, not `404`.

#### `PATCH /api/v1/categories/{category_id}`

**`tenant_admin` only.** `200` → `CategoryOut`.

```jsonc
{ "name": "Hardware", "description": "…" }   // both optional
```

**`null` means "leave unchanged", not "clear".** There is consequently **no way through this API
to reset a description back to empty** once it is set. If your edit form needs a clear button,
say so — it is a one-line change (§7.3).

Unknown keys are silently dropped. `409` on a duplicate name, `404` if out of scope.

#### `DELETE /api/v1/categories/{category_id}`

**`tenant_admin` only.** `204`, empty body.

This is a **hard delete**, not an archive — there is no `is_active` flag on categories.

`409 This category is used by open incidents and cannot be deleted.` when at least one
referencing incident is **not closed** (i.e. `open` or `in_review`). The blocking incident ids
and count are not returned, so you cannot deep-link the admin to the offenders.

> **Known defect (§7.2):** the guard only checks non-closed incidents. A category referenced
> **only by closed incidents** passes the guard and then violates a database foreign key,
> producing a non-enveloped **`500`**. Until it is fixed, treat a `500` on this endpoint as
> "category still in use" in your UI rather than as an outage.

### 4.4 Users

All six endpoints are gated to `tenant_admin` + `system_admin`, so
`403 insufficient_role` with `required: ["tenant_admin", "system_admin"]` is possible on every
one of them.

`system_admin` may act on any tenant and honours `?tenant_id=`; `tenant_admin` is confined to its
own tenant and has `?tenant_id=` **silently ignored**.

#### `GET /api/v1/users`

`200` → `Page[UserOut]`.

| Query | Values | Default |
|---|---|---|
| `role` | `system_admin` \| `tenant_admin` \| `staff` \| `reviewer` | all |
| `status` | `active` \| `inactive` | all |
| `tenant_id` | uuid — **system_admin only** | all tenants |
| `limit` / `offset` | 1..100 / ≥0 | 50 / 0 |

A `system_admin` with no `?tenant_id=` gets **every user in every tenant**, with no tenant
predicate at all. Since `UserOut` carries no `tenant_id` (§5), such a list is ambiguous — you
cannot tell which organisation a row belongs to. Pin a tenant with `?tenant_id=` if you need
that. An unknown tenant UUID is not an error; it simply matches nothing.

#### `POST /api/v1/users`

`201` → `UserOut`. Invites a user: creates the Supabase auth user and the FlowDesk row, and
Supabase emails a set-password link (§2.6). No password is returned.

```jsonc
{ "email": "sam@acme.com", "name": "Sam Reviewer", "role": "reviewer" }   // all required
```

The created user is always `status: active`. There is no `status` field in the body.

**The target tenant comes from the `?tenant_id=` *query* parameter, not the body.** A
`tenant_id` inside the JSON body is **silently discarded**, and the user is created in the
caller's own tenant with a cheerful `201`. This is the single easiest way to create a user in
the wrong organisation — put it in the query string.

**Errors**

| Status | `reason` | Message |
|---|---|---|
| 403 | `privilege_escalation` | `Only System Admins can create System Admin accounts.` |
| 409 | `—` | `A user with this email already exists in your organisation.` |
| 409 | `—` | `A user with this email already exists.` (registered globally in Supabase) |

Emails are unique per tenant, compared **case-insensitively** — so the same person can exist in
two different tenants, but not twice in one.

Same partial-compensation caveat as `POST /organizations`: a failure after the auth user is
created usually cleans up, but a timeout can leave an orphan (§7.2).

#### `GET /api/v1/users/{user_id}`

`200` → `UserOut`. `404 User not found.` for absent or another tenant's (NFR-12). There is no
way to read a user's `tenant_id` through this endpoint.

#### `PATCH /api/v1/users/{user_id}`

`200` → `UserOut`.

```jsonc
{ "name": "Sam R.", "role": "staff" }   // both optional
```

**Only `name` and `role` are patchable.** `email` cannot be changed through this API at all, and
`status` goes through the activate/deactivate endpoints below. Unknown keys are silently
dropped.

`403 privilege_escalation` — `Only System Admins can grant the System Admin role.` Note this
fires *after* the scope check, so a `tenant_admin` patching an out-of-tenant user to
`system_admin` sees `404`, not `403`.

#### `POST /api/v1/users/{user_id}/deactivate` · `POST /api/v1/users/{user_id}/activate`

**No request body.** `200` → the full `UserOut` with the new `status` — **not** `204`.

Both are idempotent: deactivating an inactive user returns `200` and the same body.

> **There is no self-targeting guard.** An admin can deactivate **their own account**, and the
> effect is immediate: their next request — including the request that would reactivate
> themselves — returns `403 account_deactivated`. They are locked out and need another admin, or
> direct database access, to recover.
>
> Please put a confirmation step in front of this in the UI. A tenant with exactly one admin who
> deactivates themselves has bricked their organisation. Tracked in §7.2 as a backend fix worth
> making, but the UI guard is cheaper and should exist regardless.

Deactivation takes effect on the user's very next request even though their JWT is still valid
(§2.1).

### 4.5 Incidents

#### `POST /api/v1/incidents`

**`staff` only** — every other role, including `tenant_admin` and `system_admin`, gets
`403 insufficient_role` with `required: ["staff"]`. `201` → the full `IncidentDetail`.

```jsonc
{
  "title": "Printer on fire",              // required, 1..255
  "description": "Third-floor printer…",   // required, 1..10000
  "category_id": "uuid",                   // must exist in the caller's tenant
  "severity": "high"                       // low | medium | high | critical
}
```

`status`, `submitted_by` and the tenant are set by the server and are not accepted from the
body. A new incident is created **`open` and unassigned**.

`422 category_not_in_tenant` — `The selected category does not exist in your organisation.` A
category belonging to another tenant produces the **identical** error to one that does not exist
anywhere, deliberately, so nothing leaks.

This is `422`, **not `404`**: the category is a body field, not the addressed resource, and a
`404` on `POST /incidents` would read as "this endpoint does not exist".

#### `GET /api/v1/incidents`

Any authenticated user, no role gate. `200` → `Page[IncidentOut]` (no `description`, no
timeline).

| Query | Values | Default |
|---|---|---|
| `status` | `open` \| `in_review` \| `closed` | all |
| `severity` | `low` \| `medium` \| `high` \| `critical` | all |
| `sort` | `created_at` \| `updated_at` \| `title` \| `severity` \| `status` | `created_at` |
| `order` | `asc` \| `desc` | `desc` |
| `tenant_id` | uuid — **system_admin only**, ignored otherwise | all tenants |
| `limit` / `offset` | 1..100 / ≥0 | 50 / 0 |

Sorting always carries a secondary `id` tiebreak, so paging is stable even when the sort key
ties.

**Visibility is enforced server-side; do not filter client-side:**

| Role | Sees |
|---|---|
| `staff` | incidents they submitted |
| `reviewer` | incidents assigned to them **plus unassigned ones** in their tenant |
| `tenant_admin` | every incident in their tenant |
| `system_admin` | every incident in every tenant |

The reviewer's unassigned arm is what makes the workflow reachable: incidents are created with
no assignee, so a strict `assigned_to == me` scope would hide every new incident from every
reviewer. Reviewers work from a shared queue and claim work by moving it to In Review.

**A detail 404 exactly matches a list omission** — both use the same predicate. If it is not in
the list, `GET /incidents/{id}` returns `404`.

One consequence to design the reviewer queue around: once an incident leaves `open`, it is
visible to **at most one** reviewer — its assignee. If a `tenant_admin` claimed it (which is
allowed, see §4.6), `assigned_to` points at a Tenant Admin and the incident is visible to
**zero** reviewers, plus that tenant's admins. A reviewer watching their queue will see it
simply vanish.

As in §4.4, a cross-tenant `system_admin` listing is ambiguous: neither `IncidentOut` nor
`IncidentDetail` exposes `tenant_id`, so you cannot label rows by organisation. Pin one tenant
with `?tenant_id=`.

#### `GET /api/v1/incidents/{incident_id}`

`200` → `IncidentDetail`: everything in `IncidentOut` plus `description`, `transitions` and
`allowed_transitions`.

```jsonc
{
  "id": "uuid",
  "title": "Printer on fire",
  "description": "…",
  "severity": "high",
  "status": "in_review",
  "category":     { "id": "uuid", "name": "Hardware" },
  "submitted_by": { "id": "uuid", "name": "…", "email": "…" },
  "assigned_to":  { "id": "uuid", "name": "…", "email": "…" },   // or null
  "created_at": "2026-07-30T…Z",
  "updated_at": "2026-07-30T…Z",
  "transitions": [
    {
      "id": "uuid",
      "from_status": "open",
      "to_status": "in_review",
      "transitioned_by": { "id": "uuid", "name": "…", "email": "…" },
      "note": null,
      "created_at": "2026-07-30T…Z"
    }
  ],
  "allowed_transitions": ["closed"]
}
```

**Three things worth knowing before you build the page:**

1. **A freshly submitted incident has `transitions: []`.** This is not a bug. The timeline
   records *state changes*, and a submission has no from-state. Render the first timeline entry
   from the incident's own `created_at` and `submitted_by`, both of which are in the payload.
2. **`allowed_transitions` is caller-dependent.** It is a function of the incident's state *and*
   the caller's role, so `staff` and `system_admin` always receive `[]`, and a closed incident
   always receives `[]`. **Do not cache it across users.** Render your workflow buttons from
   this array and you never need to duplicate the state machine in React.
3. **Reassignment does not appear in the timeline.** Same reason as (1) — the timeline row
   requires a from- and to-status. It is in the server audit log. Open question with Fady
   whether the user-visible timeline needs it, since that would require a schema change.

#### `GET /api/v1/incidents/{incident_id}/transitions`

`200` → **a bare JSON array** of `TransitionOut`.

This is the one list-ish endpoint that is **not** wrapped in `{"data": …, "pagination": …}` and
is not paginated at all. Oldest first, by `created_at` then `id`. An incident with no state
changes returns `[]`.

Same visibility rules and the same `404` as the detail endpoint.

### 4.6 Workflow

#### `POST /api/v1/incidents/{incident_id}/transitions`

**`reviewer` and `tenant_admin`** — `staff` and `system_admin` get `403 insufficient_role`.

Returns **`200` with the refreshed `IncidentDetail`** — not `201` with the transition — so one
round-trip gives you the new state, the updated timeline and the new `allowed_transitions`.
(`201` would also be a lie on an idempotent replay, where nothing is created.)

```jsonc
{ "to_status": "closed", "note": "Replaced the fuser unit." }   // note ≤2000, nullable
```

**The complete matrix.** Every cell is either a real transition, an idempotent replay, or a
`409 invalid_transition`.

| from ＼ to | `open` | `in_review` | `closed` |
|---|---|---|---|
| **`open`** | `409` | **`200`** — writes a timeline row, claims the incident if unassigned | `409`, `allowed: ["in_review"]` |
| **`in_review`** | `409`, `allowed: ["closed"]` | `200` replay — writes nothing | **`200`** — writes a timeline row, `note` **required** |
| **`closed`** | `409`, `allowed: []` | `409`, `allowed: []` | `200` replay — writes nothing |

`409 invalid_transition` always carries `from_status`, `to_status` and `allowed` — the legal
targets from the current state — so you can render a precise message without hardcoding the
machine. `allowed` is `[]` from `closed`.

An unknown `to_status` value is a framework `422`.

**Idempotency (NFR-06).** Asking for a state the incident **already occupies** returns `200`
with the current state and writes nothing — no second timeline row, no second audit line, no
notification. Two consequences to code against:

- **A replayed close does not re-validate the note.** Closing an already-closed incident with no
  note returns `200`, not `422`. Nothing is being written, so there is nothing to validate. Any
  `note` sent on a replay is discarded.
- **`open → open` is a `409`, not a replay.** `open` is the initial state and is never the target
  of a legal edge, so requesting it is not a repeat of any successful action.

**Claiming.** `open → in_review` sets `assigned_to` to the actor **only if the incident is
currently unassigned**. It never steals an existing assignee. Note the actor is not required to
be a reviewer — a `tenant_admin` claiming an incident becomes its assignee, with the visibility
consequence described in §4.5.

**Concurrency.** The status change uses a guarded conditional update. If two clients race, the
loser re-reads the incident: if the winner happened to reach the same state the loser wanted,
the loser gets `200` (the postcondition holds); otherwise it gets `409 invalid_transition` whose
`from_status` is the *winning* state and whose `allowed` reflects it. Either way the timeline
never gets a duplicate row.

`422 resolution_note_required` — `A resolution note is required to close this incident.` A
missing note, `null`, `""` and a whitespace-only `"   "` all fail identically. The note is
persisted verbatim, untrimmed.

#### `POST /api/v1/incidents/{incident_id}/assign`

**`tenant_admin` only** — `reviewer`, `staff` and `system_admin` all get
`403 insufficient_role` with `required: ["tenant_admin"]`. `200` → `IncidentDetail`.

```jsonc
{ "assigned_to": "uuid" }
```

Allowed source states are `open` and `in_review`. **The closed check runs before the assignee is
validated**, so a closed incident plus a garbage assignee returns `409`, not `422`.

| Status | `reason` | Message |
|---|---|---|
| 409 | `incident_closed` | `A closed incident cannot be reassigned.` |
| 422 | `invalid_assignee` | `The selected user is not an active reviewer in this organisation.` |

The target must be an **active reviewer in the incident's tenant**. One message covers all
failure modes — no such user, wrong tenant, wrong role, inactive — so nothing leaks.

Reassignment writes **no timeline row** (§4.5 note 3). Reassigning to the user who is already
the assignee is accepted as a `200` and still sends that user another notification and writes
another audit line — there is no no-op short-circuit, so avoid firing it on an unchanged
dropdown.

### 4.7 *(retired — was "Reserved (Sprint 3)")*

The reserved stubs are deleted. Every path that used to answer `501 not_implemented` is now
real. **Remove any 501 handling you still have.**

Three of them changed method availability permanently:

| Method | Path | Was | Now |
|---|---|---|---|
| POST | `/api/v1/notifications` | `501` | **`405 method_not_allowed`** |
| POST | `/api/v1/analytics/volume` | `501` | **`405 method_not_allowed`** |
| POST | `/api/v1/analytics/status-distribution` | `501` | **`405 method_not_allowed`** |

Notifications are **system-generated only**: the write path is a workflow transition, and a
client able to create them could forge a notification against any incident. Analytics are reads.

POST is **not** gone from the `/notifications` namespace —
`POST /notifications/{id}/read` and `POST /notifications/read-all` exist. Only the
**collection-level** POST is removed.

### 4.8 Notifications (UC-09)

**No role gate: every role receives notifications.** The only rule is **you see your own**, so
there is no `403 insufficient_role` on these routes at all — out of scope is always `404`. Not
even a System Admin can read another user's notifications; a notification is personal
correspondence, not tenant data. (`403 account_deactivated` is still reachable, as everywhere.)

#### `GET /api/v1/notifications`

`200` → `Page[NotificationOut]`.

```jsonc
{
  "id": "uuid",
  "incident_id": "uuid",          // navigate here on click
  "message": "\"Printer on fire\" is now In Review.",
  "is_read": false,
  "created_at": "2026-07-30T…Z"
}
```

| Query | Values | Default |
|---|---|---|
| `is_read` | `true` \| `false` | all — `?is_read=false` is your unread tab |
| `limit` / `offset` | 1..100 / ≥0 | 50 / 0 |

`is_read` is parsed leniently: `1`, `0`, `yes`, `no`, `on`, `off` and any casing all work as well
as `true`/`false`. Only a value outside that set — including the empty `?is_read=` — is a `422`.

`created_at` here is **UTC**, like every other timestamp in the API. It is *not* converted to the
reporting timezone the way analytics buckets are (§4.9) — format it in the viewer's local zone
client-side.

**Ordering is fixed** — newest first, with an `id` tiebreak so paging is stable — and not
caller-selectable. `sort`/`order` can be added later without breaking anything.

No `user_id` or `tenant_id` in the row: you *are* the user. No embedded incident title or status
either — the title is already inside `message`, and a stale status in a notification would
contradict the incident page. If you want `incident: {id, title, status}` embedded, say so now
rather than in Sprint 4; it is a join plus a schema, not a rewrite.

Note the human-readable state label: `in_review` renders as `In Review` inside `message`.

#### `GET /api/v1/notifications/unread-count`

`200` → `{"unread": 3}`.

A separate endpoint rather than a field on the list envelope: the bell renders on **every** page
while the panel opens rarely, so the badge should not cost a paginated query, and `Page<T>` is a
shared generic that one endpoint's convenience should not deform.

`?is_read=false` + `pagination.total` gives the same number — both are built from one predicate
and a test pins that they agree — but `limit` has a minimum of 1, so you cannot get a count that
way without also fetching a row.

#### `POST /api/v1/notifications/{notification_id}/read`

No request body. `200` → the updated `NotificationOut`.

**Idempotent**: a second call returns a byte-identical body and writes nothing.

`404 notification_not_found` — `Notification not found.` for anything you do not own. All three
causes (no such id, someone else's, another tenant's) produce a **byte-identical body**;
existence is not leaked, and there is a test asserting exactly that.

The backend marks read and returns the row — **it does not redirect.** Navigation is yours, using
`incident_id`.

#### `POST /api/v1/notifications/read-all`

No request body. `200` → `{"marked_read": 3, "unread": 0}`.

`unread` is always `0`, so you can reuse one badge-setter for this response and for
`/unread-count` — the read-all schema extends the unread-count schema. `marked_read` is how many
rows this call actually flipped, so a repeat call reports `0`.

**"Dismiss" means mark read, not delete** — the rows stay in the panel. Nothing in the SRS or
the schema supports deletion (there is no `deleted_at`), and UC-09's postcondition requires
notifications to remain accessible.

#### When notifications appear

| Event | Recipient |
|---|---|
| Workflow transition (UC-08 step 9) | the incident's **submitter** |
| Reassignment | the **new** assignee (and again on a no-op reassign, §4.6) |
| Idempotent replay, rejected transition, failed reassignment | nobody |
| An action the recipient performed themselves | nobody |
| The recipient is deactivated or their row is gone | nobody — logged server-side |

That last row is reachable in normal operation and is easy to misread as a bug: a staff member
promoted to reviewer who then transitions **their own earlier submission** generates no
notification, because the only recipient would be themselves.

Delivery is **best-effort by design** (UC-09 E1): if writing the notification fails, the
transition still succeeds and returns `200`. The failure is logged server-side; the client sees
nothing. So do not treat "transition returned 200" as proof that a notification exists.

Open question with Fady: UC-09's precondition also names the *assigned reviewer* as a recipient
of transition notifications, while UC-08 step 9 names only the submitter. The narrower reading
ships; widening it is one line.

### 4.9 Analytics (UC-10)

Both endpoints are **`tenant_admin` / `system_admin`**. `staff` and `reviewer` get
`403 insufficient_role` — these aggregate every incident in the tenant, which is strictly more
than those roles may read, so answering would leak how many incidents exist that they cannot
open.

`?tenant_id=` pins one tenant **for a System Admin** and is silently ignored for anyone else. A
System Admin with no `?tenant_id=` aggregates **across all tenants**, mixing organisations into
one number — rarely what you want on a dashboard, so pass `tenant_id`.

**Not an error: "no data yet" (UC-10 E1).** An empty organisation is a **`200`** with zero-filled
buckets and `total: 0`. Do not code a `404` branch for it — check `total === 0` (status
distribution) or `data.every(b => b.count === 0)` (volume).

#### `GET /api/v1/analytics/volume` — incidents per week (US-15)

```jsonc
{
  "data": [
    { "week_start": "2026-07-20", "count": 0 },
    { "week_start": "2026-07-27", "count": 4 }
  ],
  "timezone": "Australia/Melbourne",
  "from": "2026-07-20",
  "to": "2026-07-27",
  "severity": null
}
```

| Query | Values | Default |
|---|---|---|
| `from` | date (`YYYY-MM-DD`) | `to` − 11 weeks |
| `to` | date | today in the reporting timezone |
| `severity` | `low` \| `medium` \| `high` \| `critical` | all |
| `tenant_id` | uuid — **system_admin only** | all tenants |

There are no `limit`/`offset` params.

**Four things to build against:**

1. **This is not `Page<T>`, on purpose.** Paginating a bounded set of at most 53 buckets is
   meaningless, and the shared `limit` default of 50 would have silently truncated a 53-week
   request into a wrong-looking chart. The `data` key is kept so an "unwrap `.data`" helper still
   works; window metadata replaces `pagination`.
2. **Empty weeks come back as `count: 0`.** Do not generate the missing Mondays yourself — you
   would do it in the *browser's* timezone, and a viewer in Perth or on a UTC laptop would
   produce a different set of Mondays than the server bucketed by, so the labels would not line
   up with the bars. The week calendar is computed once, server-side.
3. **`from`/`to` echo the EFFECTIVE window**, snapped outward to whole ISO weeks (Monday-based).
   Ask for `from=2026-07-30` and you get `from=2026-07-27` back. **Label the axis from the
   response, not from what you sent** — otherwise the first and last bars are silently partial
   and the trend lies.
4. **`timezone` tells you which weeks these are.** Buckets are Melbourne weeks, not UTC weeks. An
   incident submitted Monday 09:00 Melbourne is Sunday 23:00 UTC; bucketing in UTC would file it
   a week early, every Monday morning. Show the field, or at least never assume UTC.

Weeks start **Monday** (ISO). Buckets are ordered ascending and every Monday in the window is
present exactly once.

| Status | `reason` | When |
|---|---|---|
| 422 | `invalid_date_range` | `from > to`, compared on the **raw** dates before snapping |
| 422 | `date_range_too_large` | more than 53 weeks after snapping; carries `max_weeks: 53`. Exactly 53 is accepted |
| 422 | `—` (`details.errors`) | an unparseable date, or a `severity` outside the enum |

Send `from`/`to` as `YYYY-MM-DD` strings. A bare number is *not* rejected — it is read as a Unix
timestamp, so `?from=0` silently becomes 1970-01-01 and you get `date_range_too_large` rather than
the parse error you expected.

#### `GET /api/v1/analytics/status-distribution` — current split (US-16)

```jsonc
{
  "data": [
    { "status": "open",      "count": 7 },
    { "status": "in_review", "count": 2 },
    { "status": "closed",    "count": 11 }
  ],
  "total": 20
}
```

`data` **always** has exactly three entries in workflow order (`open` → `in_review` → `closed`),
including zeros, so you never branch on a missing key. `total` is the single field to check for
the empty state.

Only `tenant_id` (system_admin) is accepted — **no date range.** UC-10 step 4 says "current
count" while UC-10 A1 implies the date picker re-renders both charts; that is a contradiction in
the SRS, and step 4's reading ships, because US-16 asks for *operational load* and a
date-windowed version answers a different question (the status of incidents *created* in that
window). Flagged with Fady; adding `from`/`to` later is a copy-paste of the volume params.

---

## 5. Data models

Field-level reference. Nullable fields are marked; everything else is always present on
responses. This mirrors `/openapi.json`, which is authoritative for shapes.

### Requests

**`OrgRegisterRequest`**

| Field | Type | Rule |
|---|---|---|
| `organization_name` | string | required, 2–255 |
| `admin_email` | string | required, email; **normalised on storage** (§4.2) |
| `admin_name` | string | required, 1–255 |

**`UserInvite`**

| Field | Type | Rule |
|---|---|---|
| `email` | string | required, email |
| `name` | string | required, 1–255 |
| `role` | enum | required. `system_admin` is accepted by the schema but rejected at runtime for non-System-Admins (`403 privilege_escalation`) |

**`UserUpdate`** — both optional; `null` means leave unchanged.

| Field | Type | Rule |
|---|---|---|
| `name` | string \| null | 1–255 |
| `role` | enum \| null | |

**`CategoryCreate`** / **`CategoryUpdate`**

| Field | Type | Rule |
|---|---|---|
| `name` | string | 1–255. Required on create, optional on update |
| `description` | string \| null | ≤ 2000, optional. On update `null` means leave unchanged, **not** clear (§4.3) |

**`IncidentCreate`**

| Field | Type | Rule |
|---|---|---|
| `title` | string | required, 1–255 |
| `description` | string | required, 1–10000 |
| `category_id` | uuid | required, must exist in the caller's tenant |
| `severity` | enum | required |

**`TransitionCreate`**

| Field | Type | Rule |
|---|---|---|
| `to_status` | enum | required |
| `note` | string \| null | ≤ 2000. Required in effect when `to_status = closed` on a real transition |

**`IncidentAssign`**

| Field | Type | Rule |
|---|---|---|
| `assigned_to` | uuid | required |

### Responses

**`MeResponse`** — `id` (uuid, = Supabase auth user id), `email`, `name`, `role` (enum),
`status` (enum), `tenant` (`TenantRef`).

**`OrgRegisterResponse`** — `tenant` (`TenantOut`), `admin_user` (`AdminUserOut`).

**`TenantOut`** — `id`, `name`, `created_at`. **`TenantRef`** — `id`, `name`.

**`AdminUserOut`** — `id`, `email`, `name`, `role`, `status`. No `created_at`.

**`UserOut`** — `id`, `email`, `name`, `role`, `status`, `created_at`. **No `tenant_id`, no
`updated_at`** (§3.3, §4.4).

**`UserRef`** — `id`, `name`, `email`. Used for `submitted_by`, `assigned_to`,
`transitioned_by`.

**`CategoryOut`** — `id`, `name`, `description` (nullable), `created_at`. No `updated_at`.

**`CategoryRef`** — `id`, `name`.

**`IncidentOut`** — `id`, `title`, `severity`, `status`, `category` (`CategoryRef`),
`submitted_by` (`UserRef`), `assigned_to` (`UserRef` \| null), `created_at`, `updated_at`. **No
`tenant_id`, no `description`.**

**`IncidentDetail`** — everything in `IncidentOut` plus `description`, `transitions`
(`TransitionOut[]`), `allowed_transitions` (`IncidentStatus[]`, caller-dependent).

**`TransitionOut`** — `id`, `from_status`, `to_status`, `transitioned_by` (`UserRef`), `note`
(nullable), `created_at`.

**`NotificationOut`** — `id`, `incident_id`, `message`, `is_read`, `created_at`. No `user_id`,
no `tenant_id`.

**`UnreadCountOut`** — `unread` (int). **`MarkAllReadOut`** — `unread`, `marked_read`.

**`VolumeBucket`** — `week_start` (date), `count` (int).

**`VolumeSeries`** — `data` (`VolumeBucket[]`), `timezone` (string), `from` (date), `to` (date),
`severity` (enum \| null — the only optional field).

**`StatusCount`** — `status` (enum), `count` (int). **`StatusDistribution`** — `data`
(`StatusCount[]`, always 3 entries), `total` (int).

**`Pagination`** — `limit`, `offset`, `total`. **`Page[T]`** — `data` (`T[]`), `pagination`.

---

## 6. Frontend integration checklist

| Item | Value / action |
|---|---|
| API base URL | `https://flowdesk-backend.fly.dev/api/v1` |
| Supabase URL | `https://tqkyghashkawgqbnrwaf.supabase.co` |
| Supabase anon key | `sb_publishable__FHOWZZTWuTAXrGhDRuMPA_Q-y7BF24` (safe in browser) |
| Auth header | `Authorization: Bearer <access_token>` on all non-public calls |
| CORS — already allowed | `http://localhost:5173`, `https://flowdesk.vanelsen.net.au` |
| New CORS origin? | Ask backend to add it (Fly config `CORS_ORIGINS`) — e.g. a Vercel preview URL |
| Public endpoints | `POST /organizations`, `GET /health` (+ `/docs`, `/redoc`, `/openapi.json`) |
| Health probe | `GET https://flowdesk-backend.fly.dev/health` → `{"status":"ok"}` |

**Verified against the live deployment on 30 July 2026:** both CORS origins answer preflight with
`allow-credentials: true` and `max-age: 600`; `/openapi.json` serves all 19 paths including
notifications and analytics; an unauthenticated `GET /me` returns exactly
`{"error":{"code":"unauthorized","message":"Missing authentication token.","details":{"reason":"missing_token"}}}`.

**Before you ship, make sure you have:**

- [ ] **`Content-Type: application/json` on the registration request** — omitting it is a
      plain-text `500` that looks like a CORS error (§3.1.4, D-13). Set it everywhere anyway; the
      `api()` helper in §2.4 already does.
- [ ] An error parser that **tolerates a non-JSON body** on `5xx` (§3.1.4). This is the one that
      will bite you in a demo.
- [ ] `401` handling that branches on `details.reason`, not on the status — and that does **not**
      force a logout on `invalid_token` (§3.1.3).
- [ ] URLs built **without** trailing slashes (§3.1.4).
- [ ] `422` handling that checks `details.reason` first, then `details.errors` — and that treats
      `loc[1]` as possibly an integer (§3.1.5).
- [ ] Role-gated navigation driven by `GET /me`, re-fetched after any role change.
- [ ] Workflow buttons rendered from `allowed_transitions`, never cached across users (§4.5).
- [ ] A confirmation dialog in front of self-deactivation (§4.4).
- [ ] The `/set-password` page live — invited users cannot log in without it (§2.6).
- [ ] Analytics axis labelled from the **response** `from`/`to`, not the request (§4.9).
- [ ] No `501` handling anywhere (§4.7).

---

## 7. Open items, known defects, and sign-off

### 7.1 Corrections to the earlier contracts

If you built against v1.0 or the Sprint deltas, these four items changed or were wrong. They are
the reason this document exists as a rewrite rather than a third delta.

1. **`verification_failed` is not the JWKS-outage slug** — `invalid_token` is. v1.0 said
   otherwise and the advice attached to it would log every user out during a Supabase incident.
   See the warning in §3.1.3.
2. **The error envelope is not universal.** v1.0 listed `500 internal_error` as an envelope row;
   there is no such envelope. `500` is plain text, and so is a CORS rejection (`400`). §3.1.4.
3. **`501 not_implemented` is dead.** v1.0 documented it for the reserved endpoints; those are
   deleted and nothing raises it. Collection-level POST on the three former stubs is now `405`.
4. **`403 insufficient_role` beats `422`**, not the other way around, because role checks run as
   dependencies before parameter validation. §3.1.6.

### 7.2 Known defects

Found while writing this contract. All are real, none is a blocker for the demo, and each has a
one-line-to-one-file fix. Listed here rather than quietly omitted, because a contract that hides
them is worse than one that names them. These double as Defect Register entries for Assessment 3.

| # | Severity | Defect |
|---|---|---|
| D-13 | **High** | **`POST /organizations` with a body but no (or a wrong) `Content-Type` returns a plain-text `500`**, not `422` or `415` — the validation handler cannot serialise the raw bytes it is handed. Confined to that one endpoint because it is the only public one with a body, but that is the registration screen. Surfaces in a browser as an opaque CORS error, since the response carries no `allow-origin` header. **Verified on production.** §3.1.4 |
| D-14 | Medium | **A `NaN`/`Infinity` literal in the `POST /organizations` body** crashes the same handler → plain-text `500` instead of `422`. **Verified on production.** §3.1.4 |
| D-1 | High | **No generic exception handler**, so any unexpected failure is a plain-text `500` outside the envelope. This is the root cause of D-13, D-14 and D-2. **Partly fixed in Sprint 4:** the Supabase Admin path — by far the most reachable, and hit in production by GoTrue's `429 over_email_send_rate_limit` — now raises typed `AppError`s and answers an enveloped `502`/`503` (§3.1.3). `SupabaseAdminError` inheriting from `Exception` was what put it outside the handler. The generic handler is still missing, so D-13, D-14 and D-2 stand. §3.1.4 |
| D-2 | High | **`DELETE /categories/{id}` returns `500`** for a category referenced only by *closed* incidents — the guard checks non-closed only, then a foreign key fails. §4.3 |
| D-3 | Medium | **Self-deactivation locks an admin out permanently.** No self-targeting guard on `/deactivate`; a sole admin can brick their tenant. §4.4 |
| D-4 | Medium | **Orphaned Supabase auth users on timeout.** Compensation covers database failures but not a timed-out invite call, permanently trapping that email on a `409`. §4.2, §4.4 |
| D-5 | Medium | **Six errors ship with `details: {}`** (`User not found.`, `Category not found.`, both incident 404s, the category and email `409`s, both organisation `409`s), so a client must match on message text. Backfilling `details.reason` is additive and safe. §3.1.3 |
| D-6 | Low | **`system_admin` cannot write categories or perform workflow actions** (`403`). Probably unintended, since it is cross-tenant everywhere else. §2.9 |
| D-7 | Low | **Cross-tenant lists are unlabelled** — `UserOut` and `IncidentOut` carry no `tenant_id`, so a System Admin's all-tenant list cannot be grouped by organisation. §4.4, §4.5 |
| D-15 | Medium | **The trailing-slash `307` redirects to `http://`, not `https://`** — the app does not honour the proxy's forwarded-proto header, so a browser blocks the redirect as mixed content while curl follows it happily. **Verified on production.** §3.1.4 |
| D-8 | Low | **`405` drops the `Allow` header.** §3.1.3 |
| D-9 | Low | **`/docs` and `/openapi.json` are public in production** — `APP_ENV` exists but is never read. §1.3 |
| D-10 | Low | **No way to clear a category description** — `null` means "unchanged". §4.3 |
| D-11 | Low | **A no-op reassign still notifies and audits.** §4.6 |
| D-12 | Low | **`GET /me` can return a `401` with no reason slug** (`User tenant not found.`) — the only one in the API. §3.1.3 |

### 7.3 Open questions

**For Fady (product decisions, all shipped with a documented default):**

1. **Who receives a transition notification** — UC-08 step 9 says the submitter; UC-09's
   precondition adds "or assigned reviewer". Shipped: submitter only.
2. **Date range on the status distribution** — UC-10 step 4 says "current"; UC-10 A1 implies the
   date picker re-renders both charts. Shipped: no date range.
3. **UC-09 E1 vs UC-09's postcondition** — E1 requires the transition to survive a notification
   failure; the postcondition requires the notification to be recorded. Shipped: best-effort, so
   the postcondition is conditionally false.
4. **Reassignment in the user-visible timeline** — currently audit-log only; adding it needs a
   schema change.
5. **Should `system_admin` be a strict superset of `tenant_admin`?** (D-6.)
6. **Priority for the defect register above** — my proposal for Sprint 4 is **D-1** (a generic
   exception handler, which also fixes D-13 and D-14), **D-2** (the category-delete `500`),
   **D-15** (the `https` redirect, a one-line proxy-header setting) and **D-3** (the
   self-deactivation lockout). Those four are the ones a tester or a demo will actually hit. The
   rest as capacity allows; **D-5** (backfilling `details.reason`) is the largest but is purely
   additive and safe to do incrementally.

**For Brad (contract confirmations):**

1. **`POST /notifications` is permanently `405`** — remove any create path you stubbed.
2. **No `GET /notifications/{id}`** — the list row *is* the full payload. Adding one is additive.
3. **`marked_read` in the read-all response** — useful, or noise?
4. **Embedded incident in `NotificationOut`** — `incident_id` only today. Ask now if you want
   `{id, title, status}`.
5. **No data is a `200`, not a `404`** — for both analytics endpoints.
6. **Do you need a "clear description" on categories?** (D-10.)
7. **Do you need `tenant_id` on `UserOut`/`IncidentOut`** for the System Admin views? (D-7.)

### 7.4 Sign-off

| | |
|---|---|
| Backend (Ivan) | Contract matches the deployed code as of 30 July 2026 ✅ |
| Frontend (Brad) | ☐ Confirm §7.3 items 1–7 |
| Product (Fady) | ☐ Confirm §7.3 items 1–6; approve making this file canonical |

Any change to the API is versioned in this file and reflected automatically in
`/openapi.json`.
