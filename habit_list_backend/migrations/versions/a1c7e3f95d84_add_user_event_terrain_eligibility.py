"""add user event terrain eligibility

Revision ID: a1c7e3f95d84
Revises: 9d4e6f8a2b10
Create Date: 2026-08-08 10:40:00

Terrain eligibility used to live only in the moments router, so the terrain
projection had to approximate it with ``source == "moment"``.  That filter also
made companion evidence permanently invisible to terrain, which contradicts the
core loop in the product baseline.  Moving the permission onto the row lets the
writer decide once and the projection read the same committed value.

Existing rows are backfilled from the old behaviour: only moment-sourced events
were ever eligible, so the previous projection stays byte-identical after the
migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e3f95d84"
down_revision: str | Sequence[str] | None = "9d4e6f8a2b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "terrain_eligible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            batch_op.f("ix_user_events_terrain_eligible"),
            ["terrain_eligible"],
            unique=False,
        )
    # Preserve the pre-migration projection exactly: the old terrain query only
    # accepted moment-sourced events.
    op.execute(
        """
        UPDATE user_events
           SET terrain_eligible = TRUE
         WHERE source = 'moment'
           AND mode = 'moment'
        """
    )
    with op.batch_alter_table("user_events", schema=None) as batch_op:
        batch_op.alter_column("terrain_eligible", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("user_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_events_terrain_eligible"))
        batch_op.drop_column("terrain_eligible")
