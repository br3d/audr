"""Foundation tables: job_run, schedule, provider_budget, worker_status,
operational_event, key_state.

Revision ID: 001
Revises: (initial)
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # ------------------------------------------------------------------
    # key_state — wrapped master-key storage (read by init_key.py)
    # ------------------------------------------------------------------
    op.create_table(
        "key_state",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name", name="pk_key_state"),
    )

    # ------------------------------------------------------------------
    # job_run — individual job executions with lease / heartbeat fencing
    # ------------------------------------------------------------------
    op.create_table(
        "job_run",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_job_run"),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','completed','failed','cancelled')",
            name="ck_job_run_status",
        ),
    )
    op.create_index("ix_job_run_kind_status", "job_run", ["kind", "status"])
    op.create_index("ix_job_run_status", "job_run", ["status"])
    op.create_index("ix_job_run_heartbeat_at", "job_run", ["heartbeat_at"])

    # Prevent two active (in_progress) runs of the same kind simultaneously.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_job_run_kind_active
        ON job_run (kind)
        WHERE status = 'in_progress'
        """
    )

    # ------------------------------------------------------------------
    # schedule — recurring job definitions
    # ------------------------------------------------------------------
    op.create_table(
        "schedule",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("cron_expr", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_schedule"),
        sa.UniqueConstraint("kind", name="uq_schedule_kind"),
    )

    # ------------------------------------------------------------------
    # provider_budget — API quota tracking per provider per period
    # ------------------------------------------------------------------
    op.create_table(
        "provider_budget",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls_used", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("calls_limit", sa.BigInteger(), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_budget"),
    )
    op.create_index(
        "ix_provider_budget_provider_period",
        "provider_budget",
        ["provider", "period_start"],
    )

    # ------------------------------------------------------------------
    # worker_status — liveness tracking for worker processes
    # ------------------------------------------------------------------
    op.create_table(
        "worker_status",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="idle"),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_job_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_worker_status"),
        sa.UniqueConstraint("worker_id", name="uq_worker_status_worker_id"),
        sa.CheckConstraint(
            "status IN ('idle','running','stopped')",
            name="ck_worker_status_status",
        ),
        sa.ForeignKeyConstraint(
            ["current_job_run_id"],
            ["job_run.id"],
            name="fk_worker_status_current_job_run_id_job_run",
            ondelete="SET NULL",
        ),
    )

    # ------------------------------------------------------------------
    # operational_event — append-only audit log for system events
    # ------------------------------------------------------------------
    op.create_table(
        "operational_event",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operational_event"),
    )
    op.create_index(
        "ix_operational_event_type_occurred",
        "operational_event",
        ["event_type", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_table("operational_event")
    op.drop_table("worker_status")
    op.drop_table("provider_budget")
    op.drop_table("schedule")
    op.drop_table("job_run")
    op.drop_table("key_state")
