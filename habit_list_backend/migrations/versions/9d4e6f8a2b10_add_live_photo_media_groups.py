"""add explicit media group metadata for Live Photo pairs

Revision ID: 9d4e6f8a2b10
Revises: 8c3e5a7b1d2f
Create Date: 2026-08-06 11:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d4e6f8a2b10"
down_revision: str | Sequence[str] | None = "8c3e5a7b1d2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("media_group_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("media_role", sa.String(length=24), nullable=True))
        batch_op.create_index(
            "ix_media_assets_media_group_id", ["media_group_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.drop_index("ix_media_assets_media_group_id")
        batch_op.drop_column("media_role")
        batch_op.drop_column("media_group_id")
