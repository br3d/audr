"""Asset news cache table (AUD-308 / AUD-301).

Persists deduplicated news articles fetched from the configured news
provider (CoinGecko Demo), linked to the asset(s) they mention.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_news",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("news_site", sa.Text(), nullable=False),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_asset_news"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_asset_news_asset"
        ),
        sa.UniqueConstraint(
            "asset_id", "source", "external_id", name="uq_asset_news_dedup"
        ),
    )

    op.create_index(
        "ix_asset_news_asset_published",
        "asset_news",
        ["asset_id", sa.text("published_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_asset_news_asset_published", table_name="asset_news")
    op.drop_table("asset_news")
