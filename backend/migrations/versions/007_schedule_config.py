"""Add schedule configuration columns and provider_purge_log table.

Revision ID: 007
Revises: 006
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Extend the existing `schedule` table ---
    op.add_column(
        "schedule",
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "schedule",
        sa.Column("paused_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "schedule",
        sa.Column("freshness_s", sa.Integer(), nullable=True),
    )
    op.add_column(
        "schedule",
        sa.Column("budget_calls_per_day", sa.Integer(), nullable=True),
    )
    op.add_column(
        "schedule",
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- Create provider_purge_log ---
    op.create_table(
        "provider_purge_log",
        sa.Column(
            "id",
            sa.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("purged_by", sa.Text(), nullable=False, server_default="owner"),
        sa.Column(
            "quote_observations_deleted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "valuation_lines_deleted",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "purged_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_purge_log"),
    )


def downgrade() -> None:
    op.drop_table("provider_purge_log")

    op.drop_column("schedule", "updated_at")
    op.drop_column("schedule", "budget_calls_per_day")
    op.drop_column("schedule", "freshness_s")
    op.drop_column("schedule", "paused_at")
    op.drop_column("schedule", "revision")
