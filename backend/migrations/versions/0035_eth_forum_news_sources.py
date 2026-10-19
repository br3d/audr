"""Give ETH two sources that publish daily (AUD-623).

The founder's report — "latest news says 16 October, today is 19" — was not a
collection failure: the worker polls every 15 minutes and other assets had
rows from the same morning. The News block is a card per asset ordered by
portfolio share, and the largest position's card was the stale one. ETH/WETH
had exactly two sources (migration 0024, mapped to both symbols by 0028):
`blog.ethereum.org` (newest post 2026-10-16) and `go-ethereum` releases
(2026-10-08). Both are first-party and high-signal; both publish about weekly.

Added, checked live at the same bar as 0031/0032 — HTTP 200, parseable body,
newest entry read off the feed:

* `ethresear.ch/latest.rss` — 30 entries, newest 2026-10-19.
* `ethereum-magicians.org/latest.rss` — 30 entries, newest 2026-10-19.

These are forum feeds, the tier 0032 deliberately declined for ENS
(`discuss.ens.domains`) on volume grounds: per-post technical traffic, not
announcements, and the one asset already on a forum feed carries several times
everything else's row count. For ETH that trade was put to the founder
explicitly and accepted — a card that moves daily with forum noise beats a
first-party card that moves weekly. Both 0024 rows stay; a geth release or an
EF post still outranks a forum thread when one lands.

Mapped to `ETH` and `WETH` both, the same pair 0028 gave the Ethereum-native
feeds, so the holding and its wrapper show the same card. The 0029/0030
triggers clear `etag`/`last_polled_at` when a feed's symbol set changes, so
these rows are polled on the next run without an operator touching anything.

No `news_tracked_asset` rows: attribution here is the explicit mapping above,
not symbol guessing in press headlines (docs/news-sources.md §9, AUD-515).

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision: str | None = "0034"
branch_labels = None
depends_on = None

# (key, tier, url, symbols)
_SEED_FEEDS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "project:ethresearch",
        "project",
        "https://ethresear.ch/latest.rss",
        ("ETH", "WETH"),
    ),
    (
        "project:ethereum-magicians",
        "project",
        "https://ethereum-magicians.org/latest.rss",
        ("ETH", "WETH"),
    ),
]


def upgrade() -> None:
    conn = op.get_bind()

    for key, tier, url, symbols in _SEED_FEEDS:
        feed_id = conn.scalar(sa.text("SELECT id FROM news_feed WHERE key = :key"), {"key": key})
        if feed_id is None:
            feed_id = uuid.uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO news_feed (id, key, tier, url) VALUES (:id, :key, :tier, :url)"
                ),
                {"id": feed_id, "key": key, "tier": tier, "url": url},
            )
        for symbol in symbols:
            conn.execute(
                sa.text(
                    "INSERT INTO news_feed_symbol (feed_id, asset_symbol) "
                    "VALUES (:feed_id, :symbol) ON CONFLICT DO NOTHING"
                ),
                {"feed_id": feed_id, "symbol": symbol},
            )


def downgrade() -> None:
    conn = op.get_bind()
    keys = [key for key, _tier, _url, _symbols in _SEED_FEEDS]
    conn.execute(sa.text("DELETE FROM news_feed WHERE key = ANY(:keys)"), {"keys": keys})
