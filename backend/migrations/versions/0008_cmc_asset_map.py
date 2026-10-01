"""CoinMarketCap address/symbol resolution tables + allow it as a quote provider (AUD-358).

`quote_refresh` previously hard-depended on a CoinGecko API key that a fresh
install never has, so `total_usd` stayed null out of the box. CoinMarketCap's
public (keyless) price endpoint only accepts numeric ids, so pricing with it
needs an address -> cmc_id resolution table, built from a vendored snapshot
of CMC's keyless `/cryptocurrency/map` (see audr.assets.cmc_catalog).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cmc_map_version",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cmc_map_version"),
        sa.UniqueConstraint("source_hash", name="uq_cmc_map_version_source_hash"),
    )

    op.create_table(
        "cmc_map_entry",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("cmc_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("eth_address", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_cmc_map_entry"),
        sa.ForeignKeyConstraint(
            ["version_id"], ["cmc_map_version.id"], name="fk_cmc_map_entry_version"
        ),
    )
    op.create_index(
        "ix_cmc_map_entry_version_address",
        "cmc_map_entry",
        ["version_id", "eth_address"],
    )
    op.create_index(
        "ix_cmc_map_entry_version_symbol",
        "cmc_map_entry",
        ["version_id", "symbol"],
    )

    # quote_set.provider was hard-pinned to 'coingecko' — allow the new
    # keyless default provider too. Raw SQL, not op.drop_constraint/
    # create_check_constraint: those re-apply the project's naming
    # convention on top of an already-fully-qualified name and double the
    # "ck_quote_set_" prefix baked into the baseline (0001) schema.
    op.execute("ALTER TABLE quote_set DROP CONSTRAINT ck_quote_set_ck_quote_set_provider")
    op.execute(
        "ALTER TABLE quote_set ADD CONSTRAINT ck_quote_set_ck_quote_set_provider"
        " CHECK (provider = ANY (ARRAY['coingecko'::text, 'coinmarketcap'::text]))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE quote_set DROP CONSTRAINT ck_quote_set_ck_quote_set_provider")
    op.execute(
        "ALTER TABLE quote_set ADD CONSTRAINT ck_quote_set_ck_quote_set_provider"
        " CHECK (provider = 'coingecko'::text)"
    )

    op.drop_index("ix_cmc_map_entry_version_symbol", table_name="cmc_map_entry")
    op.drop_index("ix_cmc_map_entry_version_address", table_name="cmc_map_entry")
    op.drop_table("cmc_map_entry")
    op.drop_table("cmc_map_version")
