"""Index incidents for the role-scoped queries introduced in Sprint 2.

Migration 0001 created only `ix_incidents_tenant_id`. PostgreSQL does not index foreign
keys automatically, so `category_id`, `submitted_by` and `assigned_to` were unindexed and
every role-scoped list (US-09) would filter them after a tenant scan. Supports NFR-01
(page load), NFR-02 (submission < 500 ms) and NFR-03 (dashboard < 2 s over 1,000
incidents).

No columns or tables change: 0001 already covers every field US-08..US-12 touches.

These index definitions are mirrored in `Incident.__table_args__`. They must stay in
sync: `alembic/env.py` diffs against `Base.metadata` (so a missing model declaration
makes the next autogenerate emit DROPs), and `tests/conftest.py` builds the test schema
with `metadata.create_all` rather than by running migrations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns) — one list so upgrade and downgrade cannot drift.
_INDEXES = [
    # Tenant Admin list + ?status= filter; also Sprint 3 US-16 status distribution.
    ("ix_incidents_tenant_status", "incidents", ["tenant_id", "status"]),
    # Staff list scope: submitted_by == caller.
    ("ix_incidents_tenant_submitted_by", "incidents", ["tenant_id", "submitted_by"]),
    # Reviewer list scope: assigned_to == caller OR NULL.
    ("ix_incidents_tenant_assigned_to", "incidents", ["tenant_id", "assigned_to"]),
    # Default sort + pagination; also Sprint 3 US-15 volume-by-week.
    ("ix_incidents_tenant_created_at", "incidents", ["tenant_id", "created_at"]),
    # Pre-existing debt: category_service.delete_category already filters on this
    # unindexed, as does the FK's ON DELETE check.
    ("ix_incidents_category_id", "incidents", ["category_id"]),
]

# No index on `severity`: four values, always combined with tenant_id — the
# tenant-leading indexes plus a filter step are sufficient.


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    # CI runs `alembic upgrade head` followed by `alembic downgrade base` as an
    # acceptance gate, so this must undo upgrade() exactly.
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
