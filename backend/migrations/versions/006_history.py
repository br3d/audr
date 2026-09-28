"""Observation invalidation, history-point summary table, and line provenance (T067 / US3 / AUD-80).

Revision ID: 006
Revises: 005
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # valuation_line.observation_id — exact provenance link to the
    # balance_observation that was current when this line was published.
    # NULL for lines created before migration 006.
    # ------------------------------------------------------------------
    op.add_column(
        "valuation_line",
        sa.Column("observation_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_valuation_line_observation_id_balance_observation",
        "valuation_line",
        "balance_observation",
        ["observation_id"],
        ["id"],
    )
    op.create_index(
        "ix_valuation_line_observation_id",
        "valuation_line",
        ["observation_id"],
    )

    # ------------------------------------------------------------------
    # balance_observation_invalidation — append-only invalidation records.
    # Raw observations are never modified; this table records that an
    # observation should no longer be treated as canonical.
    # One invalidation record per observation (UNIQUE on observation_id).
    # ------------------------------------------------------------------
    op.create_table(
        "balance_observation_invalidation",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        # reason: why the observation was invalidated
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "invalidated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # For reorgs: how many blocks were rolled back (NULL for non-reorg reasons)
        sa.Column("reorg_depth", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_balance_observation_invalidation"),
        sa.UniqueConstraint(
            "observation_id",
            name="uq_balance_observation_invalidation_observation_id",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["balance_observation.id"],
            name="fk_balance_observation_invalidation_observation_id",
        ),
        sa.CheckConstraint(
            "reason IN ('reorg', 'verification_pending')",
            name="ck_balance_observation_invalidation_reason",
        ),
    )
    op.create_index(
        "ix_balance_observation_invalidation_observation_id",
        "balance_observation_invalidation",
        ["observation_id"],
    )
    op.create_index(
        "ix_balance_observation_invalidation_invalidated_at",
        "balance_observation_invalidation",
        ["invalidated_at"],
    )

    # ------------------------------------------------------------------
    # history_point — one materialized summary row per published snapshot.
    # Written by portfolio.history.materialize_history_point() immediately
    # after a snapshot is published.  is_canonical is updated by the
    # canonicality job when it detects invalidated constituent observations.
    # ------------------------------------------------------------------
    op.create_table(
        "history_point",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # One-to-one with valuation_snapshot.
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        # Denormalised from valuation_snapshot.snapshotted_at for fast range queries.
        sa.Column(
            "snapshotted_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Aggregate USD value of the portfolio at this point (NULL when quality = stale).
        sa.Column(
            "total_value_usd",
            sa.Numeric(precision=36, scale=18),
            nullable=True,
        ),
        # Mirrors valuation_snapshot.quality.
        sa.Column("quality", sa.Text(), nullable=False),
        # Number of distinct wallets with at least one non-excluded holding line.
        sa.Column("included_wallet_count", sa.Integer(), nullable=False),
        # Number of distinct assets with at least one non-excluded holding line.
        sa.Column("included_asset_count", sa.Integer(), nullable=False),
        # True when tracked wallets/assets exist that had no observation at this point.
        sa.Column(
            "has_gap",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        # Cleared to false by the canonicality job when any constituent observation
        # is invalidated.  Written as true on first insertion.
        sa.Column(
            "is_canonical",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_history_point"),
        sa.UniqueConstraint("snapshot_id", name="uq_history_point_snapshot_id"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["valuation_snapshot.id"],
            name="fk_history_point_snapshot_id",
        ),
        sa.CheckConstraint(
            "quality IN ('complete', 'partial', 'stale', 'unknown')",
            name="ck_history_point_quality",
        ),
    )
    op.create_index(
        "ix_history_point_snapshotted_at",
        "history_point",
        ["snapshotted_at"],
    )
    op.create_index(
        "ix_history_point_snapshot_id",
        "history_point",
        ["snapshot_id"],
    )
    # Efficient range queries filtered to canonical points only.
    op.create_index(
        "ix_history_point_canonical_snapshotted_at",
        "history_point",
        ["is_canonical", "snapshotted_at"],
    )


def downgrade() -> None:
    op.drop_table("history_point")
    op.drop_table("balance_observation_invalidation")
    op.drop_index(
        "ix_valuation_line_observation_id", table_name="valuation_line"
    )
    op.drop_constraint(
        "fk_valuation_line_observation_id_balance_observation",
        "valuation_line",
        type_="foreignkey",
    )
    op.drop_column("valuation_line", "observation_id")
