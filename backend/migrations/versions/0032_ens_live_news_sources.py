"""Give ENS a source that actually publishes (AUD-568 part B, follow-up).

0031 wired ENS to `ensdomains/ens-contracts` releases and Snapshot `ens.eth`
and said so honestly: both were outside the 90-day retention window, so ENS
would show zero news until the next release or proposal. The first live run
after 0031 confirmed it — every other seeded asset picked up rows, ENS picked
up none.

"Wired but quiet" is the right answer only when nothing better exists, and
here something does. Checked live, same bar as 0031 (HTTP 200 + parseable
body, newest entry read off the feed):

* `blog.ens.domains/rss.xml` — 77 entries, newest 2026-10-14. The official
  blog; `feed.xml` and `/feed` both 404, which is presumably why the first
  pass missed it.
* `ensdomains/ensjs` releases — 10 entries, newest 2026-10-13. The contracts
  repo 0031 picked ships rarely (newest 2026-03-13); the JS client is the
  repo that actually cuts releases.

Both 0031 rows stay. `ens-contracts` and `ens.eth` are low-volume rather than
dead, and a contract release or a governance proposal is higher-signal than
either source added here when it does land.

Not added: `discuss.ens.domains/latest.rss`. It is live (30 entries, newest
2026-10-15) and a forum feed is an established tier here
(`project:aave-governance` is one), but it is per-post forum traffic rather
than project announcements, and AAVE — the one asset already on a forum feed —
carries 53 rows against everything else's 10-20. Worth revisiting if ENS
still looks thin, as a deliberate choice rather than a default.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision: str | None = "0031"
branch_labels = None
depends_on = None

# (key, tier, url, symbols)
_SEED_FEEDS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "project:ens-blog",
        "project",
        "https://blog.ens.domains/rss.xml",
        ("ENS",),
    ),
    (
        "release:ensjs",
        "release",
        "https://github.com/ensdomains/ensjs/releases.atom",
        ("ENS",),
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
