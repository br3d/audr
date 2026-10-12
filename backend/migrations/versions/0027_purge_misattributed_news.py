"""Purge news rows the pre-allow-list matcher misattributed (AUD-503).

Migration 0026 stopped the bad matching, but it left the rows that matching
had already written. On staging that was 71 of 121 `asset_news` rows: 44 on a
meme token ticked 'BITCOIN', 4 on a token named 'ASSET', and the rest on
holdings whose ticker happened to appear in a headline (ETH, USDT, USDC, T,
FOLD, ARB). Every one of them is user-visible on the wrong asset's page.

After 0026 the only paths that attribute an article to an asset are
`news_feed.asset_symbol`, `snapshot_space.asset_symbol`, and press/GDELT
matching restricted to `news_tracked_asset` — and the feed/space symbols are
themselves a subset of the allow-list. So a surviving row pointing at an asset
outside the enabled allow-list cannot have come from the fixed code: it is
pre-0026 residue, and deleting it is exactly the backfill 0026 skipped.

Deliberately not reversible. The rows were wrong; `downgrade()` recreating
them would mean re-poisoning the feed, and they come back on the next run
anyway for any symbol an operator re-adds to the allow-list.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision: str | None = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM asset_news
            WHERE asset_id IN (
                SELECT a.id
                FROM asset a
                WHERE upper(a.symbol) NOT IN (
                    SELECT upper(asset_symbol)
                    FROM news_tracked_asset
                    WHERE enabled
                )
            )
            """
        )
    )


def downgrade() -> None:
    """No-op: the deleted rows were misattributed, not data worth restoring."""
