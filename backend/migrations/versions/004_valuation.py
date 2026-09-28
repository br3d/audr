"""Quote sets, observations, and valuation snapshots/lines (T051 / US2 / AUD-64).

Revision ID: 004
Revises: 003
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # quote_set — a single batch-fetch of prices from one provider
    # ------------------------------------------------------------------
    op.create_table(
        "quote_set",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="'pending'"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quote_set"),
        sa.CheckConstraint(
            "status IN ('pending', 'complete', 'failed')",
            name="ck_quote_set_status",
        ),
        sa.CheckConstraint(
            "provider IN ('coingecko')",
            name="ck_quote_set_provider",
        ),
    )
    op.create_index("ix_quote_set_fetched_at", "quote_set", ["fetched_at"])

    # ------------------------------------------------------------------
    # quote_observation — individual price point per asset per quote_set
    # ------------------------------------------------------------------
    op.create_table(
        "quote_observation",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("quote_set_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        # Exact price in USD: 36 significant digits, 18 decimal places.
        sa.Column("price_usd", sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_quote_observation"),
        sa.UniqueConstraint(
            "quote_set_id", "asset_id", name="uq_quote_observation_set_asset"
        ),
        sa.ForeignKeyConstraint(
            ["quote_set_id"], ["quote_set.id"], name="fk_quote_observation_quote_set"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_quote_observation_asset"
        ),
        sa.CheckConstraint("price_usd > 0", name="ck_quote_observation_price_positive"),
    )
    op.create_index(
        "ix_quote_observation_set_asset",
        "quote_observation",
        ["quote_set_id", "asset_id"],
    )

    # ------------------------------------------------------------------
    # valuation_snapshot — point-in-time portfolio valuation
    # ------------------------------------------------------------------
    op.create_table(
        "valuation_snapshot",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "snapshotted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # complete = all held assets priced; partial = some missing; stale = no prices.
        sa.Column("quality", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_valuation_snapshot"),
        sa.CheckConstraint(
            "quality IN ('complete', 'partial', 'stale', 'unknown')",
            name="ck_valuation_snapshot_quality",
        ),
    )
    op.create_index(
        "ix_valuation_snapshot_snapshotted_at",
        "valuation_snapshot",
        ["snapshotted_at"],
    )
    op.create_index(
        "ix_valuation_snapshot_published_at",
        "valuation_snapshot",
        ["published_at"],
    )

    # ------------------------------------------------------------------
    # valuation_line — one holding per (wallet, asset) within a snapshot
    # ------------------------------------------------------------------
    op.create_table(
        "valuation_line",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("raw_amount", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        # NULL when price was not available at snapshot time.
        sa.Column("price_usd", sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column("value_usd", sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_valuation_line"),
        sa.UniqueConstraint(
            "snapshot_id", "wallet_id", "asset_id",
            name="uq_valuation_line_snapshot_wallet_asset",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["valuation_snapshot.id"],
            name="fk_valuation_line_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallet.id"], name="fk_valuation_line_wallet"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_valuation_line_asset"
        ),
    )
    op.create_index(
        "ix_valuation_line_snapshot_id",
        "valuation_line",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_valuation_line_wallet_asset",
        "valuation_line",
        ["wallet_id", "asset_id"],
    )


def downgrade() -> None:
    op.drop_table("valuation_line")
    op.drop_table("valuation_snapshot")
    op.drop_table("quote_observation")
    op.drop_table("quote_set")
