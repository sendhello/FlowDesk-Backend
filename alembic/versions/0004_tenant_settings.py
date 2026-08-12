"""Give tenants the two fields the workspace settings screen edits (D-18).

UC-01 had no persistence layer at all: there was no settings router and no settings model,
so the Save button on the settings page produced no request and the page reported success
regardless. This adds the storage half.

`timezone` is NOT NULL with a server default rather than nullable-means-inherit. Existing
rows are backfilled with `Australia/Melbourne`, which is the value `REPORTING_TIMEZONE` in
fly.toml already held, so the column changes nothing about how any existing tenant's
analytics are bucketed on the day it ships. The literal is repeated here rather than
imported from `app.models.tenant`: a migration has to keep describing the schema of its own
revision even after the application constant moves on.

`updated_at` mirrors `incidents.updated_at` exactly — same type, same server default, same
`onupdate` in the model — so "when was this last changed" means the same thing on both
tables.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_TIMEZONE = "Australia/Melbourne"


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default=_DEFAULT_TIMEZONE,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    # CI runs `alembic upgrade head` then `alembic downgrade base` as an acceptance gate,
    # so this must undo upgrade() exactly or 0001's drop_table runs against a schema it
    # did not leave behind.
    op.drop_column("tenants", "updated_at")
    op.drop_column("tenants", "timezone")
