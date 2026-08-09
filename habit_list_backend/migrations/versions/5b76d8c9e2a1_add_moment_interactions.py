"""add fragment-scoped moment interactions

Revision ID: 5b76d8c9e2a1
Revises: 8ce4cecf534e
Create Date: 2026-08-02 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b76d8c9e2a1"
down_revision: str | Sequence[str] | None = "8ce4cecf534e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moment_interactions",
        sa.Column("interaction_id", sa.String(length=36), nullable=False),
        sa.Column("moment_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reaction", sa.String(length=24), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["moment_id"], ["episodic.episodic_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("interaction_id"),
    )
    with op.batch_alter_table("moment_interactions", schema=None) as batch_op:
        batch_op.create_index(
            "idx_moment_interaction_thread",
            ["moment_id", "created_at", "interaction_id"],
            unique=False,
        )
        batch_op.create_index(
            "idx_moment_interaction_user_time",
            ["user_id", "created_at", "interaction_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_moment_interactions_actor"), ["actor"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_moment_interactions_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_moment_interactions_kind"), ["kind"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_moment_interactions_moment_id"), ["moment_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_moment_interactions_status"), ["status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_moment_interactions_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("moment_interactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_moment_interactions_user_id"))
        batch_op.drop_index(batch_op.f("ix_moment_interactions_status"))
        batch_op.drop_index(batch_op.f("ix_moment_interactions_moment_id"))
        batch_op.drop_index(batch_op.f("ix_moment_interactions_kind"))
        batch_op.drop_index(batch_op.f("ix_moment_interactions_created_at"))
        batch_op.drop_index(batch_op.f("ix_moment_interactions_actor"))
        batch_op.drop_index("idx_moment_interaction_user_time")
        batch_op.drop_index("idx_moment_interaction_thread")
    op.drop_table("moment_interactions")
