"""add cron_jobs (ADR-0019, планировщик)

Revision ID: f6b8d3e2a9c4
Revises: e5a9c4d1b8f3
Create Date: 2026-07-21 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6b8d3e2a9c4"
down_revision: str | None = "e5a9c4d1b8f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cron_jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("schedule_kind", sa.String(length=32), nullable=False),
        sa.Column("schedule_spec", sa.String(length=64), nullable=False),
        sa.Column("tz", sa.String(length=64), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("workspace", sa.String(length=1024), nullable=False),
        sa.Column("session_id", sa.String(length=32), nullable=True),
        sa.Column("autonomy", sa.String(length=32), nullable=False),
        sa.Column("config_digest", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=255), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_cron_jobs_enabled", "cron_jobs", ["enabled"], unique=False)
    op.create_index("ix_cron_jobs_next_run_at", "cron_jobs", ["next_run_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cron_jobs_next_run_at", table_name="cron_jobs")
    op.drop_index("ix_cron_jobs_enabled", table_name="cron_jobs")
    op.drop_table("cron_jobs")
