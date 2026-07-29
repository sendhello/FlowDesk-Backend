# FlowDesk API Contract — Sprint 2 delta

**For:** Bradley Van Elsen (Frontend Lead) · **From:** Ivan Bazhenov (Backend Lead)
**Covers:** US-08, US-09, US-10, US-11, US-12 (UC-06, UC-07, UC-08, UC-11)
**Status:** implemented and merged; live at `/docs` and `/openapi.json`

This is the source text for the Sprint 2 sections of `FlowDesk_API_Contract.docx`. It is
markdown so it can be reviewed in the same pull request that changes the API — the `.docx`
is binary and cannot be diffed. Proposal for Fady: make this file canonical and generate
the Word document from it.

Everything in Sprint 1's contract still holds: base path `/api/v1`, `Authorization:
Bearer <supabase jwt>`, and the single error envelope
`{"error": {"code", "message", "details"}}`.

---

## §3.1 — New error reasons

Errors **we** raise always carry `details.reason`. Shape errors caught by FastAPI's own
validation (unknown enum value, `limit=0`, missing field) keep the already-documented
`details.errors` shape. That is why `sort` and `order` are enums rather than free strings:
bad values are rejected natively, so there is no extra reason slug to branch on.

| HTTP | `details.reason` | Message | When |
|---|---|---|---|
| 409 | `invalid_transition` | This transition is not permitted from the current state. | UC-08 E1 |
| 409 | `incident_closed` | A closed incident cannot be reassigned. | UC-08 A1 |
| 422 | `resolution_note_required` | A resolution note is required to close this incident. | UC-08 E2 |
| 422 | `category_not_in_tenant` | The selected category does not exist in your organisation. | US-08 |
| 422 | `invalid_assignee` | The selected user is not an active reviewer in this organisation. | UC-08 A1 |

`invalid_transition` also carries `from_status`, `to_status` and `allowed` (the legal
targets from the current state), so you can render a precise message without hardcoding
the machine.

Note `category_not_in_tenant` is **422, not 404**: the category is a body field, not the
addressed resource, and a 404 on `POST /incidents` would read as "this endpoint does not
exist". A category belonging to another tenant produces the identical error to a category
that does not exist anywhere — deliberate, so nothing about other tenants leaks.

## §3.3 — New data types

- `severity`: `low` | `medium` | `high` | `critical` (this order is also the sort order)
- `incident_status`: `open` | `in_review` | `closed` (likewise)

---

## §4.5 — Incidents

### `POST /api/v1/incidents` — submit (UC-06, US-08)

Role: **staff only**. `201` with the full detail body.

```jsonc
// request
{
  "title": "Printer on fire",              // required, 1..255
  "description": "Third-floor printer...", // required, 1..10000
  "category_id": "uuid",                   // must exist in the caller's tenant
  "severity": "high"
}
```

`status`, `submitted_by` and the tenant are set by the server and are not accepted from
the body. A new incident is created **unassigned**.

### `GET /api/v1/incidents` — list (UC-07, US-09)

Returns the standard page envelope `{"data": [...], "pagination": {...}}` of `IncidentOut`
(no `description`, no timeline).

| Query | Values | Default |
|---|---|---|
| `status` | `open` \| `in_review` \| `closed` | all |
| `severity` | `low` \| `medium` \| `high` \| `critical` | all |
| `sort` | `created_at` \| `updated_at` \| `title` \| `severity` \| `status` | `created_at` |
| `order` | `asc` \| `desc` | `desc` |
| `tenant_id` | uuid — **system_admin only**, ignored otherwise | all tenants |
| `limit` | 1..100 | 50 |
| `offset` | >= 0 | 0 |

Visibility is enforced server-side; you do not need to filter client-side:

| Role | Sees |
|---|---|
| `staff` | incidents they submitted |
| `reviewer` | incidents assigned to them **plus unassigned ones** in their tenant |
| `tenant_admin` | every incident in their tenant |
| `system_admin` | every incident in every tenant |

The reviewer's unassigned arm is what makes the workflow reachable: UC-06 creates
incidents with no assignee, so a strict `assigned_to == me` scope would hide every new
incident from every reviewer. Reviewers therefore work from a shared queue and claim work
by moving it to In Review.

**A detail 404 exactly matches a list omission** — both use the same predicate. If it is
not in the list, `GET /incidents/{id}` returns 404.

### `GET /api/v1/incidents/{id}` — detail (UC-11, US-12)

`IncidentOut` plus `description`, `transitions` and `allowed_transitions`.

```jsonc
{
  "id": "uuid",
  "title": "Printer on fire",
  "description": "...",
  "severity": "high",
  "status": "in_review",
  "category":     { "id": "uuid", "name": "Hardware" },
  "submitted_by": { "id": "uuid", "name": "...", "email": "..." },
  "assigned_to":  { "id": "uuid", "name": "...", "email": "..." },  // or null
  "created_at": "2026-07-29T…Z",
  "updated_at": "2026-07-29T…Z",
  "transitions": [
    {
      "id": "uuid",
      "from_status": "open",
      "to_status": "in_review",
      "transitioned_by": { "id": "uuid", "name": "...", "email": "..." },
      "note": null,
      "created_at": "2026-07-29T…Z"
    }
  ],
  "allowed_transitions": ["closed"]
}
```

`GET /api/v1/incidents/{id}/transitions` returns just the `transitions` array, same
visibility rules.

**Three things worth knowing before you build the page:**

1. **A freshly submitted incident has `transitions: []`.** This is not a bug. The
   `workflow_transitions` table records *state changes*, and a submission has no
   from-state. Render the first timeline entry from the incident's own `created_at` and
   `submitted_by`, both of which are in the payload.
2. **`allowed_transitions` is caller-dependent.** It is a function of the incident's state
   *and* the caller's role, so `staff` always receives `[]` and a closed incident always
   receives `[]`. Do not cache it across users. Render your workflow buttons from this
   array and you will never need to duplicate the state machine in React.
3. **Reassignment does not appear in the timeline.** Same NOT NULL reason as (1). It is in
   the server audit log; open question with Fady whether the user-visible timeline needs
   it, since that would require a schema change.

---

## §4.6 — Workflow (new section)

### `POST /api/v1/incidents/{id}/transitions` (UC-08, US-10/11)

Roles: **reviewer, tenant_admin**. Returns **`200` with the refreshed `IncidentDetail`**
(not 201 + the transition), so one round-trip gives you the new state, the updated
timeline and the new `allowed_transitions` — UC-08 step 10 is "refresh the incident detail
page".

```jsonc
{ "to_status": "closed", "note": "Replaced the fuser unit." }
```

| from ＼ to | `open` | `in_review` | `closed` |
|---|---|---|---|
| **`open`** | 409 | **200** (claims the incident if unassigned) | 409 — UC-08 E1 |
| **`in_review`** | 409 | 200 replay | **200**, `note` required — UC-08 E2 |
| **`closed`** | 409 | 409 | 200 replay |

An unknown `to_status` value is a 422 from FastAPI's enum validation.

### Idempotency (NFR-06) — read this bit

The endpoint is **idempotent in effect though POST in method**. The path was frozen in the
Sprint 1 contract, so the verb did not change; the semantics are what matter:

> Idempotency is defined over the postcondition, not over the request.

Asking for a state the incident **already occupies** returns `200` with the current state
and writes nothing — no second timeline row, no second audit line, no notification. So a
double-click, a retried request after a dropped connection, or a duplicate submission from
a flaky network are all safe: you get the same body back and the timeline stays correct.

Two consequences to code against:

- **A replayed close does not re-validate the note.** Closing an already-closed incident
  with no note returns `200`, not `422`. Nothing is being written, so there is nothing to
  validate. Any `note` sent on a replay is discarded.
- **`open -> open` is a 409, not a replay.** `open` is the initial state and is never the
  target of a legal edge, so requesting it is not a repeat of any successful action.

### `POST /api/v1/incidents/{id}/assign` (UC-08 A1)

Role: **tenant_admin**. Body `{"assigned_to": "uuid"}`, returns `200` + `IncidentDetail`.
The target must be an **active reviewer in the incident's tenant**; anything else is `422
invalid_assignee` with one message covering all failure modes. A closed incident is `409
incident_closed`.

---

## §4.7 — Reserved (Sprint 3)

`GET|POST /notifications`, `/analytics/volume`, `/analytics/status-distribution` still
return `501 not_implemented`. `/incidents` and `/incidents/{id}/transitions` are **no
longer reserved** — remove any 501 handling you have for those paths.

---

## §7 — Open items

Closed since Sprint 1: *"Reserved endpoints (§4.5) will be specified before Sprint 2"* —
done, above.

Still open, all three for you to confirm:

1. The empty timeline on a newly created incident (see §4.5 note 1) — confirm the
   creation entry rendered from `created_at`/`submitted_by` is enough for the UI.
2. `allowed_transitions` is caller-dependent and must not be cached across users.
3. A `403 insufficient_role` on a cross-tenant probe by a Staff user is a role-gate
   artefact, not an existence leak: the role check runs before the handler, so the 403 is
   a pure function of the caller's role and is identical for real and invented ids. Past
   the role gate, out-of-scope is always `404`.
