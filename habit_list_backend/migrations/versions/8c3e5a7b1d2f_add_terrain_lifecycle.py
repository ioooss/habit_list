"""add versioned terrain lifecycle fields

Revision ID: 8c3e5a7b1d2f
Revises: 7a1f2c9d4e6b
Create Date: 2026-08-04 19:25:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c3e5a7b1d2f"
down_revision: str | Sequence[str] | None = "7a1f2c9d4e6b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("memory_claims", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("terrain_state", sa.String(length=24), nullable=False, server_default="forming")
        )
        batch_op.add_column(sa.Column("terrain_user_label", sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column("terrain_first_revealed_at", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("terrain_last_changed_at", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("terrain_history_json", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.create_index(
            batch_op.f("ix_memory_claims_terrain_state"), ["terrain_state"], unique=False
        )
    with op.batch_alter_table("memory_claims", schema=None) as batch_op:
        batch_op.alter_column("terrain_state", server_default=None)
        batch_op.alter_column("terrain_history_json", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("memory_claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memory_claims_terrain_state"))
        batch_op.drop_column("terrain_history_json")
        batch_op.drop_column("terrain_last_changed_at")
        batch_op.drop_column("terrain_first_revealed_at")
        batch_op.drop_column("terrain_user_label")
        batch_op.drop_column("terrain_state")
