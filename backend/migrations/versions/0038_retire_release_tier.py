"""Retire the release tier; give MATIC a real news source (AUD-640 follow-up).

0037 split the release tier in two and kept the feeds that ship protocol
software, on the theory that "a bor release is a node operator's news". The
founder reopened AUD-640 against exactly those kept rows:

    v2.10.3 · v2.10.2 · v2.10.1-hotfix2 · v2.10.1-hotfix

That is the whole content of a release-tier row: a version tag. A GitHub
`releases.atom` entry carries no headline — the title *is* the tag — so even
a genuine, stable, security-relevant release renders as a line a portfolio
owner cannot read anything out of. 0037 filtered the obviously-worthless
shapes and left this one standing; it is the same complaint.

So the tier goes, not another pattern. Disabled here (`enabled = false`, rows
kept for history and a one-UPDATE reversal):

| Feed | Symbols | What the asset keeps |
|---|---|---|
| `release:polygon-bor` | MATIC | `project:polygon-forum-announcements`, added below |
| `release:go-ethereum` | ETH, WETH | `project:ethereum-blog`, `ethereum-magicians`, `ethresearch` |
| `release:ens-contracts` | ENS | `project:ens-blog`, Snapshot `ens.eth` |
| `release:uniswap-v3-core` | UNI | `project:uniswap-mirror` |
| `release:aave-v3-core` | AAVE | `project:aave-governance` |
| `release:chainlink` | LINK | `project:chainlink-blog` |

MATIC was the one asset whose *only* source was a release feed, so disabling
without a replacement would have emptied its card. Checked live, same bar as
0031/0032 (HTTP 200, parseable body, newest entry read off the feed):

* `forum.polygon.technology/c/announcements/6.rss` — 25 entries, newest
  2026-10-16, Discourse. Titles are sentences: "Bor v2.10.3 Release Mainnet
  and Testnet POS", "Security release review – Heimdall v0.12.1 (Lugano HF)",
  "Polygon PoS RPC Incident — Post-Incident Summary". This is the same
  information the bor tags were gesturing at, written for a reader. Added.

Not added, and why — the dead ends are the expensive part to re-derive:

* `polygon.technology/rss.xml`, `/blog/rss.xml` — 404 (Astro site, no feed).
* `blog.polygon.technology/feed/` — 301 to the HTML blog, no feed behind it.
* `forum.polygon.technology/latest.rss` — live, but the unmoderated firehose:
  "Best 10 Real Estate CRM Development Companies in Columbus" sits between
  the Heimdall releases. Category feeds are the moderated slice.
* `/c/polygon-improvement-proposals/80.rss` — same SEO spam leaks in.
* `/c/polygon-pos-updates/18.rss` — clean but stale, newest 2026-03-18.

Stored release rows are deleted rather than left to age out over 90 days: the
report is about what the card shows now. `news_alert.asset_news_id` is
ON DELETE CASCADE (0033), so alerts raised off a purged row go with it.

`is_noisy_release()` in `jobs/news.py` stays as a backstop — nothing in the
registry is release-tier after this, but if a future feed is, the filter
should still be in the path. The `ck_news_feed_tier` check keeps allowing
'release' for the same reason.

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision: str | None = "0037"
branch_labels = None
depends_on = None

_RETIRED_TIER = "release"

# (key, tier, url, symbols)
_SEED_FEEDS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "project:polygon-forum-announcements",
        "project",
        "https://forum.polygon.technology/c/announcements/6.rss",
        ("MATIC",),
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

    conn.execute(
        sa.text("UPDATE news_feed SET enabled = false WHERE tier = :tier"),
        {"tier": _RETIRED_TIER},
    )
    conn.execute(
        sa.text("DELETE FROM asset_news WHERE source LIKE :prefix"),
        {"prefix": f"{_RETIRED_TIER}:%"},
    )


def downgrade() -> None:
    conn = op.get_bind()
    # 0037 left `release:ensjs` and `release:kyberswap-interface` disabled, and
    # they must stay disabled — re-enable only what this migration turned off.
    conn.execute(
        sa.text(
            "UPDATE news_feed SET enabled = true WHERE tier = :tier AND key <> ALL(:keep_disabled)"
        ),
        {"tier": _RETIRED_TIER, "keep_disabled": ["release:ensjs", "release:kyberswap-interface"]},
    )
    keys = [key for key, _tier, _url, _symbols in _SEED_FEEDS]
    conn.execute(sa.text("DELETE FROM news_feed WHERE key = ANY(:keys)"), {"keys": keys})
