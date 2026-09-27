"""Wallet, asset, catalog, discovery, and balance observation tables (T031 / US1).

Revision ID: 003
Revises: 002
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # wallet — tracked Ethereum addresses (lowercase-normalised)
    # ------------------------------------------------------------------
    op.create_table(
        "wallet",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default="''"),
        sa.Column("status", sa.Text(), nullable=False, server_default="'active'"),
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
        sa.PrimaryKeyConstraint("id", name="pk_wallet"),
        sa.UniqueConstraint("address", name="uq_wallet_address"),
        sa.CheckConstraint("address = lower(address)", name="ck_wallet_address_lower"),
        sa.CheckConstraint(
            "status IN ('active', 'stopped')", name="ck_wallet_status"
        ),
    )

    # ------------------------------------------------------------------
    # asset — known ERC-20 tokens (lowercase-normalised address)
    # ------------------------------------------------------------------
    op.create_table(
        "asset",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("decimals", sa.SmallInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_asset"),
        sa.UniqueConstraint("token_address", name="uq_asset_token_address"),
        sa.CheckConstraint(
            "token_address = lower(token_address)", name="ck_asset_address_lower"
        ),
        sa.CheckConstraint(
            "source IN ('catalog', 'manual')", name="ck_asset_source"
        ),
    )

    # ------------------------------------------------------------------
    # asset_metadata_revision — append-only history of metadata changes
    # ------------------------------------------------------------------
    op.create_table(
        "asset_metadata_revision",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("decimals", sa.SmallInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_asset_metadata_revision"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_asset_metadata_revision_asset"
        ),
    )
    op.create_index(
        "ix_asset_metadata_revision_asset_id",
        "asset_metadata_revision",
        ["asset_id"],
    )

    # ------------------------------------------------------------------
    # catalog_version — pinned catalog commit snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "catalog_version",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("commit_hash", sa.Text(), nullable=False),
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_version"),
        sa.UniqueConstraint("commit_hash", name="uq_catalog_version_commit"),
    )

    # ------------------------------------------------------------------
    # catalog_entry — individual ERC-20 entries in a catalog version
    # ------------------------------------------------------------------
    op.create_table(
        "catalog_entry",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("decimals", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_entry"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["catalog_version.id"], name="fk_catalog_entry_version"
        ),
        sa.CheckConstraint(
            "token_address = lower(token_address)", name="ck_catalog_entry_address_lower"
        ),
    )
    op.create_index(
        "ix_catalog_entry_version_address",
        "catalog_entry",
        ["version_id", "token_address"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # monitored_pair — wallet × asset pairs explicitly tracked
    # ------------------------------------------------------------------
    op.create_table(
        "monitored_pair",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monitored_pair"),
        sa.UniqueConstraint("wallet_id", "asset_id", name="uq_monitored_pair"),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallet.id"], name="fk_monitored_pair_wallet"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_monitored_pair_asset"
        ),
    )

    # ------------------------------------------------------------------
    # discovery_coverage — last-scanned timestamps per wallet
    # ------------------------------------------------------------------
    op.create_table(
        "discovery_coverage",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column(
            "scanned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_discovery_coverage"),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallet.id"], name="fk_discovery_coverage_wallet"
        ),
    )
    op.create_index(
        "ix_discovery_coverage_wallet_id",
        "discovery_coverage",
        ["wallet_id"],
    )

    # ------------------------------------------------------------------
    # balance_observation — append-only balance snapshots (T038)
    # ------------------------------------------------------------------
    op.create_table(
        "balance_observation",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        # raw_amount stored as TEXT to avoid numeric precision loss for large integers
        sa.Column("raw_amount", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_balance_observation"),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallet.id"], name="fk_balance_observation_wallet"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_balance_observation_asset"
        ),
    )
    op.create_index(
        "ix_balance_observation_wallet_asset",
        "balance_observation",
        ["wallet_id", "asset_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_table("balance_observation")
    op.drop_table("discovery_coverage")
    op.drop_table("monitored_pair")
    op.drop_table("catalog_entry")
    op.drop_table("catalog_version")
    op.drop_table("asset_metadata_revision")
    op.drop_table("asset")
    op.drop_table("wallet")
