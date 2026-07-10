"""add run workspace + heartbeat (ADR-0015 §0.5)

Revision ID: d4b7f2a9c1e5
Revises: c3a2e5b8f1d4
Create Date: 2026-07-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7f2a9c1e5"
down_revision: str | None = "c3a2e5b8f1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("workspace", sa.String(length=1024), nullable=True))
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.create_index("ix_runs_workspace", "runs", ["workspace"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_workspace", table_name="runs")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "workspace")
