"""add skill_proposals

Revision ID: b2f1c7a4d9e0
Revises: a181df665c80
Create Date: 2026-07-08 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f1c7a4d9e0"
down_revision: str | None = "a181df665c80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_proposals",
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("skill_name", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "merged",
                "rejected",
                "failed",
                name="skillproposalstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("base", sa.String(length=255), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("checks", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_proposals_run_id", "skill_proposals", ["run_id"], unique=False)
    op.create_index(
        "ix_skill_proposals_skill_name", "skill_proposals", ["skill_name"], unique=False
    )
    op.create_index("ix_skill_proposals_status", "skill_proposals", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_skill_proposals_status", table_name="skill_proposals")
    op.drop_index("ix_skill_proposals_skill_name", table_name="skill_proposals")
    op.drop_index("ix_skill_proposals_run_id", table_name="skill_proposals")
    op.drop_table("skill_proposals")
