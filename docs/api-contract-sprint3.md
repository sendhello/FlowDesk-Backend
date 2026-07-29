# FlowDesk API Contract — Sprint 3 delta

**For:** Bradley Van Elsen (Frontend Lead) · **From:** Ivan Bazhenov (Backend Lead)
**Covers:** US-13, US-14, US-15, US-16 (UC-09, UC-10)
**Status:** implemented; live at `/docs` and `/openapi.json`

Source text for the Sprint 3 sections of `FlowDesk_API_Contract.docx`, in markdown so it
can be reviewed in the same pull request that changes the API. Everything in the Sprint 1
and Sprint 2 contracts still holds: base path `/api/v1`, `Authorization: Bearer <supabase
jwt>`, the single error envelope `{"error": {"code", "message", "details"}}`, and the page
envelope `{"data": [...], "pagination": {...}}`.

---

## §3.1 — New error reasons

| HTTP | `details.reason` | Message | When |
|---|---|---|---|
| 404 | `notification_not_found` | Notification not found. | `POST /notifications/{id}/read` — no such notification, someone else's, or another tenant's |
| 422 | `invalid_date_range` | The start date must not be after the end date. | `?from` > `?to` on `/analytics/volume` |
| 422 | `date_range_too_large` | The requested date range is too large. Request at most 53 weeks. | more than 53 buckets; also carries `details.max_weeks` |

All three 404 causes produce a **byte-identical body** — existence is not leaked (NFR-12).
There is a test asserting exactly that.

One new `error.code` on a different axis (Starlette emits no `details` for these):

| HTTP | `error.code` | When |
|---|---|---|
| 405 | `method_not_allowed` | `POST /notifications`, `POST /analytics/volume`, `POST /analytics/status-distribution` |

**Not an error: "no data yet" (UC-10 E1).** An empty organisation is a **`200`** with
zero-filled buckets and `total: 0`. Do not code a 404 branch for it — check
`total === 0` (status distribution) or `data.every(b => b.count === 0)` (volume).

---

## §4.7 — Reserved endpoints: **removed**

`routes/reserved.py` is deleted. Every path that used to answer `501 not_implemented` is
now real, so **remove any 501 handling you still have.**

Three of them changed method availability permanently:

| Method | Path | Was | Now |
|---|---|---|---|
| POST | `/api/v1/notifications` | `501` | **`405 method_not_allowed`** |
| POST | `/api/v1/analytics/volume` | `501` | **`405 method_not_allowed`** |
| POST | `/api/v1/analytics/status-distribution` | `501` | **`405 method_not_allowed`** |

Notifications are **system-generated only**: the write path is a workflow transition, and a
client able to create them could forge a notification against any incident. Analytics are
reads.

Note POST is **not** gone from the `/notifications` namespace — `POST
/notifications/{id}/read` and `POST /notifications/read-all` exist. Only the
**collection-level** POST is removed.

---

## §4.8 — Notifications (UC-09; US-13, US-14)

No role gate: every role receives notifications. The only rule is **you see your own**, so
there is no `403` on these routes at all — out of scope is always `404`. Not even a System
Admin can read another user's notifications; a notification is personal correspondence, not
tenant data.

### `GET /api/v1/notifications` — the panel

Standard page envelope of `NotificationOut`.

```jsonc
{
  "id": "uuid",
  "incident_id": "uuid",          // UC-09 step 7 navigates here
  "message": "\"Printer on fire\" is now In Review.",
  "is_read": false,
  "created_at": "2026-07-30T…Z"
}
```

| Query | Values | Default |
|---|---|---|
| `is_read` | `true` \| `false` | all — `?is_read=false` is your unread tab |
| `limit` | 1..100 | 50 |
| `offset` | >= 0 | 0 |

**Ordering is fixed** (newest first) and not caller-selectable: UC-09 step 5 specifies one
order. `sort`/`order` can be added later without breaking anything.

No `user_id` or `tenant_id` in the row — you *are* the user. No embedded incident title or
status either: the title is already inside `message`, and a stale status in a notification
would contradict the incident page. If you want `incident: {id, title, status}` embedded,
say so now rather than in Sprint 4 — it is a join plus a schema, not a rewrite.

### `GET /api/v1/notifications/unread-count` — the bell badge

```jsonc
{ "unread": 3 }
```

A separate endpoint rather than a field on the list envelope: the bell renders on **every**
page while the panel opens rarely, so the badge should not cost a paginated query, and
`Page<T>` is a shared generic that one endpoint's convenience should not deform.

`?is_read=false` + `pagination.total` gives the same number — both are built from one
predicate and a test pins that they agree — but `limit` has `ge=1`, so you cannot get a
count without also fetching a row.

### `POST /api/v1/notifications/{id}/read`

`200` + the updated `NotificationOut`. **Idempotent**: a second call returns the same body
and writes nothing. `404 notification_not_found` for anything you do not own.

The backend marks read and returns the row — **it does not redirect**. UC-09 step 7
conflates "mark read" and "navigate"; navigation is yours, using `incident_id`.

### `POST /api/v1/notifications/read-all` — dismiss all (UC-09 A1)

```jsonc
{ "marked_read": 3, "unread": 0 }
```

`unread` is always `0` (UC-09 A1.2), so you can reuse one badge-setter for this response
and for `/unread-count` — `MarkAllReadOut` extends `UnreadCountOut`. `marked_read` is how
many rows this call actually flipped; tell me if you do not want it.

"Dismiss" means **mark read, not delete** — the rows stay in the panel. Nothing in the SRS
or the schema supports deletion (there is no `deleted_at`), and UC-09's postcondition
requires notifications to remain accessible.

### When notifications appear

| Event | Recipient |
|---|---|
| Workflow transition (UC-08 step 9) | the incident's **submitter** |
| Reassignment (UC-08 A1.2) | the **new** reviewer |
| Idempotent replay, rejected transition, failed reassignment | nobody |
| An action the recipient performed themselves | nobody |

Open question with Fady: UC-09's precondition also names the *assigned reviewer* as a
recipient of transition notifications, while UC-08 step 9 names only the submitter. I have
shipped the narrower reading; widening it is one line.

---

## §4.9 — Analytics (UC-10; US-15, US-16)

Both endpoints are **`tenant_admin` / `system_admin`**. Staff and reviewer get `403
insufficient_role` — these aggregate every incident in the tenant, which is strictly more
than those roles may read, so answering would leak how many incidents exist that they
cannot open.

`?tenant_id=` pins one tenant **for a System Admin** and is silently ignored for anyone
else — same behaviour as `GET /incidents` and `GET /users`.

### `GET /api/v1/analytics/volume` — incidents per week (US-15)

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
| `severity` | `low` \| `medium` \| `high` \| `critical` | all (UC-10 step 5) |
| `tenant_id` | uuid — **system_admin only** | all tenants |

**Four things to build against:**

1. **This is not `Page<T>`, on purpose.** Paginating a bounded set of at most 53 buckets is
   meaningless, and the shared `limit` default of 50 would have silently truncated a
   53-week request into a wrong-looking chart. The `data` key is kept so an "unwrap
   `.data`" helper still works; window metadata replaces `pagination`.
2. **Empty weeks come back as `count: 0`.** Do not generate the missing Mondays yourself —
   you would do it in the *browser's* timezone, and a viewer in Perth or on a UTC laptop
   would produce a different set of Mondays than the server bucketed by, so the labels
   would not line up with the bars. The week calendar is computed once, server-side.
3. **`from`/`to` echo the EFFECTIVE window**, snapped outward to whole ISO weeks (Monday
   based). Ask for `from=2026-07-30` and you get `from=2026-07-27` back. Label the axis
   from the response, not from what you sent — otherwise the first and last bars are
   silently partial and the trend lies.
4. **`timezone` tells you which weeks these are.** Buckets are Melbourne weeks, not UTC
   weeks. An incident submitted Monday 09:00 Melbourne is Sunday 23:00 UTC; bucketing in
   UTC would file it a week early, every Monday morning. Show the field, or at least never
   assume UTC.

Ranges beyond 53 weeks are `422 date_range_too_large` (with `details.max_weeks`), and
`from > to` is `422 invalid_date_range`. An unparseable date is FastAPI's native `422` with
`details.errors`, same as a bad enum today.

### `GET /api/v1/analytics/status-distribution` — current split (US-16)

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

`data` **always** has exactly three entries in workflow order (`open` → `in_review` →
`closed`), including zeros, so you never branch on a missing key. `total` is the single
field to check for UC-10 E1's empty state.

Only `tenant_id` (system_admin) is accepted — **no date range**. UC-10 step 4 says
"current count" while UC-10 A1 implies the date picker re-renders both charts; that is a
contradiction in the SRS and I have shipped step 4's reading, because US-16 asks for
*operational load* and a date-windowed version answers a different question (the status of
incidents *created* in that window). Flagged with Fady; adding `from`/`to` later is a
copy-paste of the volume params.

---

## §7 — Open items

Closed since Sprint 2: all three notes on the incident detail (empty timeline,
caller-dependent `allowed_transitions`, the 403-vs-404 role-gate artefact) still stand and
need no action.

New, for you to confirm:

1. **`POST /notifications` is permanently 405** — remove any create path you stubbed.
2. **No `GET /notifications/{id}`** — the list row *is* the full payload. Adding one later
   is additive; say if you want it now.
3. **`marked_read` in the read-all response** — useful, or noise?
4. **Embedded incident in `NotificationOut`** — `incident_id` only today. Ask now if you
   want `{id, title, status}`.
5. **No data is a `200`, not a `404`** — for both analytics endpoints.
