"""Remove dead cron_expr column from schedule table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("schedule", "cron_expr")


def downgrade() -> None:
    op.add_column(
        "schedule",
        sa.Column("cron_expr", sa.Text(), nullable=False, server_default=""),
    )
