"""Index notifications for the panel and badge queries introduced in Sprint 3.

INCIDENTS NEED NOTHING HERE, and that is deliberate rather than an oversight. Migration
0002 already created `(tenant_id, created_at)` — an exact prefix for US-15's volume query
(tenant equality plus a created_at range) — and `(tenant_id, status)`, an exact match for
US-16's grouped count. 0002's own comments predicted both.

Notifications are a different story: 0001 gave them only single-column
`ix_notifications_tenant_id` and `ix_notifications_user_id`, while every real query is
(user_id, something):

    panel         : WHERE user_id = ? [AND is_read = ?] ORDER BY created_at DESC, id DESC
    unread badge  : SELECT count(*) WHERE user_id = ? AND is_read = false
    read-all      : UPDATE ...       WHERE user_id = ? AND is_read = false
    mark-one-read : WHERE id = ? AND user_id = ?      (primary key — already covered)

`ix_notifications_user_id` becomes a STRICT PREFIX of both new composites once they exist,
so it is dropped: every INSERT would otherwise keep paying to maintain it. By contrast
`ix_notifications_tenant_id` is a prefix of nothing, no query needs it and no query is
harmed by it, so it stays — schema is not churned without a reason.

REJECTED: a partial index `(user_id) WHERE is_read = false`. It is the textbook fit for
the badge (small, and it shrinks as users read), but Alembic's autogenerate compares
partial-index predicates by rendered WHERE text and is a known source of spurious diffs.
`alembic check` staying clean is a hard requirement in CI, so the plain composite wins.

These index definitions are mirrored in `Notification.__table_args__`. They must stay in
sync: `alembic/env.py` diffs against `Base.metadata` (so a missing model declaration makes
the next autogenerate emit DROPs), and `tests/conftest.py` builds the test schema with
`metadata.create_all` rather than by running migrations.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns) — one list so upgrade and downgrade cannot drift.
_INDEXES = [
    ("ix_notifications_user_created_at", "notifications", ["user_id", "created_at"]),
    ("ix_notifications_user_is_read", "notifications", ["user_id", "is_read"]),
]

# Redundant once the composites above exist: a strict leading prefix of both.
_REDUNDANT = ("ix_notifications_user_id", "notifications", ["user_id"])


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)
    op.drop_index(_REDUNDANT[0], table_name=_REDUNDANT[1])


def downgrade() -> None:
    # CI runs `alembic upgrade head` then `alembic downgrade base` as an acceptance gate,
    # so this must undo upgrade() exactly — including recreating the index it dropped, or
    # 0001's drop_table would run against a schema it did not leave behind.
    op.create_index(_REDUNDANT[0], _REDUNDANT[1], _REDUNDANT[2])
    for name, table, _ in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
