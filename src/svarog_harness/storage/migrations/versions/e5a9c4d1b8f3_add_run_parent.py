"""add run parent_run_id (ADR-0015 фаза 3, child runs)

Revision ID: e5a9c4d1b8f3
Revises: d4b7f2a9c1e5
Create Date: 2026-07-11 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a9c4d1b8f3"
down_revision: str | None = "d4b7f2a9c1e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("parent_run_id", sa.String(length=36), nullable=True))
    op.create_index("ix_runs_parent_run_id", "runs", ["parent_run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_parent_run_id", table_name="runs")
    op.drop_column("runs", "parent_run_id")
