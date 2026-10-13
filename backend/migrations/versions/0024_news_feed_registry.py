"""Keyless news feed registry — RSS/Atom + GitHub releases.atom (AUD-512).

`news_refresh` depended on a CoinGecko API key and returned early without
one, which is the bug the founder reported on AUD-503. This table is the
source of truth mapping held assets to their project blogs and GitHub
release feeds (docs/news-sources.md §2 tiers 'project'/'release'), plus a
handful of portfolio-wide trade-press feeds (tier 'press', matched by
keyword like the old CoinGecko job did). `asset_symbol` is a plain text
match key rather than a foreign key to `asset`: the registry is seeded
ahead of time for well-known tokens, independent of whether that asset has
actually been discovered into the `asset` table yet on this install.

No schema change to `asset_news` itself — only the new registry table, with
health/validator columns per AUD-512's acceptance criteria ("a feed that
404s or returns HTML must be visibly stale, not silently dead").

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None

# (key, tier, asset_symbol, url)
# project/release feeds cover the well-known assets already seeded for test
# fixtures (tests/fixtures/seed.py); press feeds are portfolio-wide and
# matched by keyword against held assets, same as the retired CoinGecko job.
_SEED_FEEDS: list[tuple[str, str, str | None, str]] = [
    # Tier: project (first-party blogs)
    ("project:ethereum-blog", "project", "WETH", "https://blog.ethereum.org/en/feed.xml"),
    ("project:uniswap-mirror", "project", "UNI", "https://mirror.xyz/uniswap.eth/feed/atom"),
    ("project:aave-governance", "project", "AAVE", "https://governance.aave.com/latest.rss"),
    ("project:chainlink-blog", "project", "LINK", "https://blog.chain.link/feed/"),
    # Tier: release (GitHub releases.atom — ships before the press does)
    (
        "release:go-ethereum",
        "release",
        "WETH",
        "https://github.com/ethereum/go-ethereum/releases.atom",
    ),
    (
        "release:uniswap-v3-core",
        "release",
        "UNI",
        "https://github.com/Uniswap/v3-core/releases.atom",
    ),
    (
        "release:aave-v3-core",
        "release",
        "AAVE",
        "https://github.com/aave/aave-v3-core/releases.atom",
    ),
    (
        "release:chainlink",
        "release",
        "LINK",
        "https://github.com/smartcontractkit/chainlink/releases.atom",
    ),
    # Tier: press (portfolio-wide breadth, matched by keyword)
    ("press:coindesk", "press", None, "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("press:cointelegraph", "press", None, "https://cointelegraph.com/rss"),
    ("press:decrypt", "press", None, "https://decrypt.co/feed"),
    ("press:the-defiant", "press", None, "https://thedefiant.io/feed"),
    ("press:bitcoin-magazine", "press", None, "https://bitcoinmagazine.com/feed"),
]


def upgrade() -> None:
    op.create_table(
        "news_feed",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("asset_symbol", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # Conditional-GET validators (AUD-512 req. 2) — null until the first poll.
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        # Per-feed self-throttle (AUD-512 req. 2: "per-source rather than global").
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        # Health counters (AUD-512 req. 5).
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
        sa.PrimaryKeyConstraint("id", name="pk_news_feed"),
        sa.UniqueConstraint("key", name="uq_news_feed_key"),
        sa.CheckConstraint(
            "tier = ANY (ARRAY['project'::text, 'release'::text, 'press'::text])",
            name="ck_news_feed_tier",
        ),
    )
    op.create_index(
        "ix_news_feed_enabled_asset_symbol",
        "news_feed",
        ["enabled", "asset_symbol"],
    )

    news_feed = sa.table(
        "news_feed",
        sa.column("id", sa.Uuid()),
        sa.column("key", sa.Text()),
        sa.column("tier", sa.Text()),
        sa.column("asset_symbol", sa.Text()),
        sa.column("url", sa.Text()),
    )
    op.bulk_insert(
        news_feed,
        [
            {
                "id": uuid.uuid4(),
                "key": key,
                "tier": tier,
                "asset_symbol": asset_symbol,
                "url": url,
            }
            for key, tier, asset_symbol, url in _SEED_FEEDS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_news_feed_enabled_asset_symbol", table_name="news_feed")
    op.drop_table("news_feed")
