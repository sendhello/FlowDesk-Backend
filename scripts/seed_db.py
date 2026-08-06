"""The only part of the seeder that talks to PostgreSQL directly.

Everything else goes through the public API on purpose. Two things cannot:

1. **Purge.** The API exposes no delete for tenants, users, incidents or notifications —
   by design, since nothing in the product deletes them. Re-running the seeder therefore
   needs SQL.
2. **Timestamps.** `incidents.created_at` and `workflow_transitions.created_at` are set by
   the server (`server_default=now()`); no request body can influence them. Without a
   backfill every seeded incident lands in the current week and `/analytics/volume`
   (UC-10, 12 weekly buckets) renders a single bar — the endpoint would be live but
   undemonstrable.

Both operations are confined to this module so the non-API surface of the run is exactly
one file. Both run in a single transaction, and the backfill verifies its own postcondition
*before* committing: if any invariant fails the transaction rolls back and the database is
left exactly as the API produced it.

`updated_at` deserves a note. The ORM declares `onupdate=func.now()`, but SQLAlchemy applies
that client-side at flush time — migration `0001_initial_schema` creates no trigger, and
`grep -i trigger alembic/versions/*.py` finds nothing. A raw UPDATE therefore does NOT
silently re-stamp the column, which is what makes writing a historical `updated_at` here
possible at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg

# `users` is deleted after `incidents` even though the tenant cascade could handle it:
# incidents.submitted_by -> users.id is NO ACTION, and relying on PostgreSQL's cascade
# ordering to satisfy it is a footgun. Explicit order, leaf tables first.
_PURGE_ORDER: tuple[tuple[str, str], ...] = (
    ("notifications", "DELETE FROM notifications WHERE tenant_id = ANY($1::uuid[])"),
    (
        "workflow_transitions",
        "DELETE FROM workflow_transitions WHERE incident_id IN "
        "(SELECT id FROM incidents WHERE tenant_id = ANY($1::uuid[]))",
    ),
    ("incidents", "DELETE FROM incidents WHERE tenant_id = ANY($1::uuid[])"),
    ("categories", "DELETE FROM categories WHERE tenant_id = ANY($1::uuid[])"),
    ("users", "DELETE FROM users WHERE tenant_id = ANY($1::uuid[])"),
    ("tenants", "DELETE FROM tenants WHERE id = ANY($1::uuid[])"),
)

_COUNT_QUERIES: tuple[tuple[str, str], ...] = (
    ("tenants", "SELECT count(*) FROM tenants WHERE id = ANY($1::uuid[])"),
    ("users", "SELECT count(*) FROM users WHERE tenant_id = ANY($1::uuid[])"),
    ("categories", "SELECT count(*) FROM categories WHERE tenant_id = ANY($1::uuid[])"),
    ("incidents", "SELECT count(*) FROM incidents WHERE tenant_id = ANY($1::uuid[])"),
    (
        "workflow_transitions",
        "SELECT count(*) FROM workflow_transitions WHERE incident_id IN "
        "(SELECT id FROM incidents WHERE tenant_id = ANY($1::uuid[]))",
    ),
    (
        "notifications",
        "SELECT count(*) FROM notifications WHERE tenant_id = ANY($1::uuid[])",
    ),
)


class SeedDbError(RuntimeError):
    """A direct-database step failed its own verification."""


def to_asyncpg_dsn(database_url: str) -> str:
    """Strip SQLAlchemy's `+asyncpg` dialect marker; asyncpg wants a plain libpq URL."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def connect(database_url: str) -> asyncpg.Connection:
    """Open one connection.

    `statement_cache_size=0` is mandatory: DATABASE_URL points at Supabase's pooler, and
    prepared statements do not survive a transaction-mode pool.
    """
    return await asyncpg.connect(to_asyncpg_dsn(database_url), statement_cache_size=0)


# ---- Preflight --------------------------------------------------------------------


async def schema_version(conn: asyncpg.Connection) -> str | None:
    return await conn.fetchval("SELECT version_num FROM alembic_version")


async def tenant_ids_by_name(
    conn: asyncpg.Connection, names: list[str]
) -> dict[str, uuid.UUID]:
    """Resolve seeded tenants by EXACT name.

    Exact equality, never `LIKE`: purge must be incapable of matching a tenant the seeder
    did not create, whatever someone later names their organisation.
    """
    rows = await conn.fetch(
        "SELECT id, name FROM tenants WHERE name = ANY($1::text[])", names
    )
    return {r["name"]: r["id"] for r in rows}


async def purge_preview(
    conn: asyncpg.Connection, tenant_ids: list[uuid.UUID]
) -> dict[str, int]:
    """Row counts that `purge` would delete. Read-only — this is the confirmation gate."""
    if not tenant_ids:
        return {name: 0 for name, _ in _COUNT_QUERIES}
    return {name: await conn.fetchval(q, tenant_ids) for name, q in _COUNT_QUERIES}


async def purge(
    conn: asyncpg.Connection, tenant_ids: list[uuid.UUID]
) -> dict[str, int]:
    """Delete every row belonging to the given tenants. One transaction, leaves first."""
    if not tenant_ids:
        return {name: 0 for name, _ in _PURGE_ORDER}
    deleted: dict[str, int] = {}
    async with conn.transaction():
        for table, statement in _PURGE_ORDER:
            status = await conn.execute(statement, tenant_ids)
            deleted[table] = int(status.rsplit(" ", 1)[-1])
    return deleted


# ---- Backfill ---------------------------------------------------------------------


@dataclass(frozen=True)
class IncidentStamp:
    incident_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RowStamp:
    """A `workflow_transitions` or `notifications` row and the instant it should carry."""

    row_id: uuid.UUID
    created_at: datetime


async def backfill_timestamps(
    conn: asyncpg.Connection,
    *,
    incidents: list[IncidentStamp],
    transitions: list[RowStamp],
    notifications: list[RowStamp],
    tenant_ids: list[uuid.UUID],
) -> dict[str, int]:
    """Rewrite seeded timestamps to the planned history.

    Verification runs inside the transaction, so a failed invariant rolls the whole pass
    back rather than leaving the data half-historical.
    """
    result: dict[str, int] = {}
    async with conn.transaction():
        result["incidents"] = await _update_incidents(conn, incidents)
        result["workflow_transitions"] = await _update_created_at(
            conn, "workflow_transitions", transitions
        )
        result["notifications"] = await _update_created_at(
            conn, "notifications", notifications
        )

        expected = {
            "incidents": len(incidents),
            "workflow_transitions": len(transitions),
            "notifications": len(notifications),
        }
        for table, want in expected.items():
            if result[table] != want:
                raise SeedDbError(
                    f"{table}: updated {result[table]} rows, planned {want}. "
                    "Rolled back — the plan and the database disagree."
                )
        await _verify(conn, tenant_ids)
    return result


async def _update_incidents(
    conn: asyncpg.Connection, stamps: list[IncidentStamp]
) -> int:
    if not stamps:
        return 0
    status = await conn.execute(
        """
        UPDATE incidents AS i
           SET created_at = v.created_at,
               updated_at = v.updated_at
          FROM unnest($1::uuid[], $2::timestamptz[], $3::timestamptz[])
               AS v(id, created_at, updated_at)
         WHERE i.id = v.id
        """,
        [s.incident_id for s in stamps],
        [s.created_at for s in stamps],
        [s.updated_at for s in stamps],
    )
    return int(status.rsplit(" ", 1)[-1])


async def _update_created_at(
    conn: asyncpg.Connection, table: str, stamps: list[RowStamp]
) -> int:
    """UPDATE `<table>.created_at` from a plan.

    `table` is interpolated because PostgreSQL has no parameter slot for an identifier;
    it is never caller-supplied — the only two values are the literals passed above.
    """
    if not stamps:
        return 0
    status = await conn.execute(
        f"""
        UPDATE {table} AS t
           SET created_at = v.created_at
          FROM unnest($1::uuid[], $2::timestamptz[]) AS v(id, created_at)
         WHERE t.id = v.id
        """,
        [s.row_id for s in stamps],
        [s.created_at for s in stamps],
    )
    return int(status.rsplit(" ", 1)[-1])


async def _verify(conn: asyncpg.Connection, tenant_ids: list[uuid.UUID]) -> None:
    """Postcondition checks. Anything that fails aborts the enclosing transaction."""
    checks: tuple[tuple[str, str], ...] = (
        (
            "incident updated_at earlier than created_at",
            "SELECT count(*) FROM incidents "
            "WHERE tenant_id = ANY($1::uuid[]) AND updated_at < created_at",
        ),
        (
            "incident created in the future",
            "SELECT count(*) FROM incidents "
            "WHERE tenant_id = ANY($1::uuid[]) AND created_at > now()",
        ),
        (
            "transition outside its incident's lifetime",
            "SELECT count(*) FROM workflow_transitions w "
            "JOIN incidents i ON i.id = w.incident_id "
            "WHERE i.tenant_id = ANY($1::uuid[]) "
            "AND (w.created_at < i.created_at OR w.created_at > i.updated_at)",
        ),
        (
            "notification outside its incident's lifetime",
            "SELECT count(*) FROM notifications n "
            "JOIN incidents i ON i.id = n.incident_id "
            "WHERE i.tenant_id = ANY($1::uuid[]) "
            "AND (n.created_at < i.created_at OR n.created_at > i.updated_at)",
        ),
        (
            "transitions out of chronological order within an incident",
            "SELECT count(*) FROM ("
            "  SELECT incident_id, created_at,"
            "         lag(created_at) OVER ("
            "             PARTITION BY incident_id ORDER BY created_at, id) AS prev"
            "    FROM workflow_transitions"
            "   WHERE incident_id IN ("
            "         SELECT id FROM incidents WHERE tenant_id = ANY($1::uuid[]))"
            ") s WHERE prev IS NOT NULL AND created_at <= prev",
        ),
    )
    failures = []
    for label, query in checks:
        bad = await conn.fetchval(query, tenant_ids)
        if bad:
            failures.append(f"{label}: {bad} row(s)")
    if failures:
        raise SeedDbError(
            "Backfill verification failed, rolling back:\n  - " + "\n  - ".join(failures)
        )


async def seeded_notifications(
    conn: asyncpg.Connection, incident_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    """`(id, message)` per incident, in write order.

    Notifications are system-generated: the API never returns their ids (they are a side
    effect of a transition, and there is deliberately no POST /notifications), and the
    panel is recipient-scoped, so no single caller can enumerate them all. Reading them
    back is the only way to stamp them — and it is a read; the write still went through
    the API.

    The message comes back too because the caller matches each notification to the event
    that produced it by message shape rather than by position: notification_service's
    templates are stable ("… is now In Review." / "You have been assigned …"), whereas
    position would depend on `(created_at, id)` ordering with a random-UUID tiebreak.
    """
    rows = await conn.fetch(
        "SELECT id, incident_id, message FROM notifications "
        "WHERE incident_id = ANY($1::uuid[]) ORDER BY incident_id, created_at, id",
        incident_ids,
    )
    grouped: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = {}
    for row in rows:
        grouped.setdefault(row["incident_id"], []).append((row["id"], row["message"]))
    return grouped
