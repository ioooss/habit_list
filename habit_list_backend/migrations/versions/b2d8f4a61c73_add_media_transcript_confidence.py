"""add media transcript confidence

Revision ID: b2d8f4a61c73
Revises: a1c7e3f95d84
Create Date: 2026-08-08 12:20:00

A voice-only fragment used to become terrain evidence straight from its machine
transcript, so the formation layer could infer something about a person from a
sentence they had never seen, let alone verified.  Storing what the ASR provider
vouched for lets ``memory_v3_min_asr_confidence`` be enforced at the point where
the permission is granted.

Existing rows are left NULL on purpose.  NULL means "the provider reported
nothing", which is not a low score and must not be backfilled with an optimistic
one: those transcripts really are unverified, and the gate should treat them so.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2d8f4a61c73"
down_revision: str | Sequence[str] | None = "a1c7e3f95d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.add_column(sa.Column("transcript_confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_assets", schema=None) as batch_op:
        batch_op.drop_column("transcript_confidence")
