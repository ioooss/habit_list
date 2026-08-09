"""add user-owned local media assets

Revision ID: 7a1f2c9d4e6b
Revises: 5b76d8c9e2a1
Create Date: 2026-08-04 19:10:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a1f2c9d4e6b"
down_revision: str | Sequence[str] | None = "5b76d8c9e2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("asset_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("asset_kind", sa.String(length=16), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("owner_type", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("deleted_at", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.create_index("idx_media_user_owner", ["user_id", "owner_type", "owner_id"], unique=False)
        batch_op.create_index("idx_media_user_status_time", ["user_id", "status", "created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_asset_kind"), ["asset_kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_owner_id"), ["owner_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_owner_type"), ["owner_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_sha256"), ["sha256"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_media_assets_user_id"), ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        for name in (
            "ix_media_assets_user_id",
            "ix_media_assets_status",
            "ix_media_assets_sha256",
            "ix_media_assets_owner_type",
            "ix_media_assets_owner_id",
            "ix_media_assets_created_at",
            "ix_media_assets_asset_kind",
            "idx_media_user_status_time",
            "idx_media_user_owner",
        ):
            batch_op.drop_index(name)
    op.drop_table("media_assets")
