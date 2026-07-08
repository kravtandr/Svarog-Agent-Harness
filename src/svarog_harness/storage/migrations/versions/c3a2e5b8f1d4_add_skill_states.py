"""add skill_states

Revision ID: c3a2e5b8f1d4
Revises: b2f1c7a4d9e0
Create Date: 2026-07-08 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a2e5b8f1d4"
down_revision: str | None = "b2f1c7a4d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_states",
        sa.Column("skill_name", sa.String(length=128), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "active",
                "stale",
                "archived",
                name="skilllifecyclestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_skill_states_skill_name", "skill_states", ["skill_name"], unique=True)
    op.create_index("ix_skill_states_status", "skill_states", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_skill_states_status", table_name="skill_states")
    op.drop_index("ix_skill_states_skill_name", table_name="skill_states")
    op.drop_table("skill_states")
