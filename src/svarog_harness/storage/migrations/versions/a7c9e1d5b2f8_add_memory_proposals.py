"""add memory_proposals

Revision ID: a7c9e1d5b2f8
Revises: f6b8d3e2a9c4
Create Date: 2026-07-23 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1d5b2f8"
down_revision: str | None = "f6b8d3e2a9c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_proposals",
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "applied",
                "rejected",
                "failed",
                name="memoryproposalstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "origin",
            sa.Enum("dream", name="memoryproposalorigin", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("memory_head", sa.String(length=64), nullable=True),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("applied_change_ids", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_proposals_run_id", "memory_proposals", ["run_id"], unique=False)
    op.create_index("ix_memory_proposals_status", "memory_proposals", ["status"], unique=False)
    op.create_index("ix_memory_proposals_origin", "memory_proposals", ["origin"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_memory_proposals_origin", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_status", table_name="memory_proposals")
    op.drop_index("ix_memory_proposals_run_id", table_name="memory_proposals")
    op.drop_table("memory_proposals")
