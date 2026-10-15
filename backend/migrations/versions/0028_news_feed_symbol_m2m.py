"""Many-to-many feed-to-asset mapping — ETH inherits Ethereum feeds (AUD-571).

`news_feed.asset_symbol` was a scalar column, so a feed could target exactly
one asset. `project:ethereum-blog` and `release:go-ethereum` (migration 0024)
were pinned to WETH only — ETH, the owner's #2 holding by value, got zero
news from either feed despite both already being polled every run.

Re-pointing the column at ETH instead of WETH would just move the gap.
`news_tracked_asset` keyword matching (migration 0026) is not a substitute
either: ETH is a short, common ticker and would reproduce the exact
collision AUD-515 fixed (a meme token ticked BITCOIN absorbing 44 of 121
press rows). Coverage has to stay an explicit mapping, never ticker
guessing (docs/news-sources.md §9) — so a feed needs to be able to map to
more than one symbol.

`news_feed_symbol(feed_id, asset_symbol)` replaces the scalar column.
`asset_symbol IS NULL` on `news_feed` meant "press tier, matched by
allow-list" — that signal now means "no rows in `news_feed_symbol` for this
feed", preserved by moving only the existing non-null values across and
never inserting a row for a NULL.

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision: str | None = "0027"
branch_labels = None
depends_on = None

# Ethereum-native feeds seeded by migration 0024 against WETH only.
_ETH_INHERITING_FEED_KEYS = ("project:ethereum-blog", "release:go-ethereum")


def upgrade() -> None:
    op.create_table(
        "news_feed_symbol",
        sa.Column("feed_id", sa.Uuid(), nullable=False),
        sa.Column("asset_symbol", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("feed_id", "asset_symbol", name="pk_news_feed_symbol"),
        sa.ForeignKeyConstraint(
            ["feed_id"], ["news_feed.id"], name="fk_news_feed_symbol_feed", ondelete="CASCADE"
        ),
    )
    op.create_index("ix_news_feed_symbol_asset_symbol", "news_feed_symbol", ["asset_symbol"])

    op.execute(
        """
        INSERT INTO news_feed_symbol (feed_id, asset_symbol)
        SELECT id, asset_symbol FROM news_feed WHERE asset_symbol IS NOT NULL
        """
    )

    op.drop_index("ix_news_feed_enabled_asset_symbol", table_name="news_feed")
    op.drop_column("news_feed", "asset_symbol")
    op.create_index("ix_news_feed_enabled", "news_feed", ["enabled"])

    _keys = ", ".join(f"'{key}'" for key in _ETH_INHERITING_FEED_KEYS)
    op.execute(
        f"""
        INSERT INTO news_feed_symbol (feed_id, asset_symbol)
        SELECT id, 'ETH' FROM news_feed WHERE key IN ({_keys})
        ON CONFLICT DO NOTHING
        """  # noqa: S608 -- _ETH_INHERITING_FEED_KEYS is a hardcoded module constant, not user input
    )


def downgrade() -> None:
    """Best-effort: a feed mapped to more than one symbol collapses to one.

    No feed had more than one symbol before this migration ran, except the
    ETH rows it adds itself — those are the ones a downgrade is expected to
    discard, same as 0027's non-reversible purge.
    """
    op.add_column("news_feed", sa.Column("asset_symbol", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE news_feed
        SET asset_symbol = (
            SELECT min(asset_symbol) FROM news_feed_symbol
            WHERE news_feed_symbol.feed_id = news_feed.id
        )
        """
    )
    op.drop_index("ix_news_feed_enabled", table_name="news_feed")
    op.drop_table("news_feed_symbol")
    op.create_index("ix_news_feed_enabled_asset_symbol", "news_feed", ["enabled", "asset_symbol"])
