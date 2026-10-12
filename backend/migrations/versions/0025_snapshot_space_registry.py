"""Snapshot governance space registry — asset -> space mapping (AUD-513).

Maps a held governance token's symbol to the Snapshot Hub space that votes
on it, so `news_refresh`'s Snapshot tier matches by explicit mapping rather
than by symbol keyword (docs/news-sources.md §3: keyword matching on "UNI"
can be fooled by an unrelated headline containing the word "uni"). Seeded
for the governance tokens the `news_feed` registry (migration 0024) already
covers and that currently run Snapshot governance: Uniswap and Aave.

No schema change to `asset_news` — `source="snapshot"` / `external_id` =
Snapshot proposal id fits the existing `uq_asset_news_dedup` constraint.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision: str | None = "0024"
branch_labels = None
depends_on = None

# (asset_symbol, space_id)
_SEED_SPACES: list[tuple[str, str]] = [
    ("UNI", "uniswap"),
    ("AAVE", "aave.eth"),
]


def upgrade() -> None:
    op.create_table(
        "snapshot_space",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_symbol", sa.Text(), nullable=False),
        sa.Column("space_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Per-space self-throttle, same discipline as news_feed (AUD-512).
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        # Health counters (AUD-513 req. 4 — reuse the AUD-512 pattern).
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_snapshot_space"),
        sa.UniqueConstraint("asset_symbol", "space_id", name="uq_snapshot_space_asset_space"),
    )
    op.create_index(
        "ix_snapshot_space_enabled_asset_symbol",
        "snapshot_space",
        ["enabled", "asset_symbol"],
    )

    snapshot_space = sa.table(
        "snapshot_space",
        sa.column("id", sa.Uuid()),
        sa.column("asset_symbol", sa.Text()),
        sa.column("space_id", sa.Text()),
    )
    op.bulk_insert(
        snapshot_space,
        [
            {"id": uuid.uuid4(), "asset_symbol": asset_symbol, "space_id": space_id}
            for asset_symbol, space_id in _SEED_SPACES
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_snapshot_space_enabled_asset_symbol", table_name="snapshot_space")
    op.drop_table("snapshot_space")
