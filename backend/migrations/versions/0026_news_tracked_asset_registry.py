"""News tracked-asset allow-list + dead feed fix (AUD-515).

Staging surfaced the failure mode docs/news-sources.md §3 already called out
for Snapshot: keyword matching against *every* held asset is fooled by an
unrelated holding whose ticker or name collides with an English word or a
real coin's name. `match_assets()` was running over all 70 staging holdings,
so a meme token literally ticked 'BITCOIN' absorbed 44 of 121 ingested rows
and a token named 'ASSET' absorbed 4 more — every trade-press article that
merely mentions Bitcoin, or uses the word "asset", landed on the wrong
holding. The same unfiltered symbol list fed the GDELT breadth tier,
producing meaningless queries (`query=BITCOIN`, `query=T`, `query=COW`).

`news_tracked_asset` is the explicit allow-list: only symbols an operator (or
this seed) has deliberately opted in are eligible for portfolio-wide press
matching or a GDELT query. Held-ness alone is no longer sufficient — the same
"mapping, not keyword guessing" principle `snapshot_space` (migration 0025)
already applies. Seeded with the four symbols the `news_feed` registry
(migration 0024) already curates project/release coverage for.

Also fixes the one dead feed migration 0024 seeded: `project:uniswap-mirror`
(`mirror.xyz/uniswap.eth/feed/atom`) sits behind Cloudflare and 403s/fails to
parse from the staging host. `https://gov.uniswap.org/latest.rss` is the
working replacement, verified HTTP 200 `application/rss+xml` — the same
governance-forum pattern `project:aave-governance` already uses.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None

_SEED_TRACKED_SYMBOLS: list[str] = ["WETH", "UNI", "AAVE", "LINK"]

_DEAD_FEED_KEY = "project:uniswap-mirror"
_DEAD_FEED_URL = "https://gov.uniswap.org/latest.rss"


def upgrade() -> None:
    op.create_table(
        "news_tracked_asset",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_symbol", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_tracked_asset"),
        sa.UniqueConstraint("asset_symbol", name="uq_news_tracked_asset_symbol"),
    )

    news_tracked_asset = sa.table(
        "news_tracked_asset",
        sa.column("id", sa.Uuid()),
        sa.column("asset_symbol", sa.Text()),
    )
    op.bulk_insert(
        news_tracked_asset,
        [{"id": uuid.uuid4(), "asset_symbol": symbol} for symbol in _SEED_TRACKED_SYMBOLS],
    )

    op.execute(
        sa.text(
            """
            UPDATE news_feed
            SET url = :url, consecutive_failures = 0, last_error = NULL
            WHERE key = :key
            """
        ).bindparams(url=_DEAD_FEED_URL, key=_DEAD_FEED_KEY)
    )


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE news_feed SET url = :url WHERE key = :key").bindparams(
            url="https://mirror.xyz/uniswap.eth/feed/atom",
            key=_DEAD_FEED_KEY,
        )
    )
    op.drop_table("news_tracked_asset")
