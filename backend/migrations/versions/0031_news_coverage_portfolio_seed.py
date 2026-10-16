"""Extend news coverage to the rest of the real portfolio (AUD-568 part B).

Migration 0024 seeded coverage for exactly four symbols — WETH, UNI, AAVE,
LINK — chosen because the test fixtures already used them. The founder's
report on AUD-568 ("news only exists for weth, aave, uni") is that seed never
having been grown to the ~60 assets actually held. The largest hole was KNC
at roughly 65% of portfolio value with no source at all.

Coverage stays an explicit mapping, never ticker guessing (docs/news-sources.md
§9, AUD-515) — every row below is a source checked live before seeding: HTTP
200 plus a parseable body for feeds, a `proposalsCount` via Snapshot's GraphQL
API for spaces.

Deliberately NOT seeded:

* `curve.eth` Snapshot space — 203 proposals but the newest was created
  2022-04-29, well outside the 90-day `NEWS_RETENTION` window. Polling it
  every run can only ever yield zero rows; CRV is covered by its blog, which
  is live.
* SUSHI release feed — `sushiswap/sushiswap` and `sushi-labs/sushiswap`
  both serve a valid but entry-less `releases.atom`. SUSHI gets its Snapshot
  space only.
* FOLD ($12.4k) and OMG ($6.5k) — no blog, no releases, and Snapshot spaces
  with 1 and 0 proposals respectively. There is nothing to poll, so no row
  here pretends otherwise.
* The sub-$1 dust positions (ARB, APE, 1INCH, BAL, COW, COMP, DYDX) — their
  Snapshot spaces exist, but coverage there is completeness for its own sake
  against positions worth under a dollar.

Two of the seeded sources are wired but quiet today: `ens-contracts`'s newest
release is 2026-03-13 and `ens.eth`'s newest proposal is ~96 days old, both
outside retention. They will populate on the next release or proposal rather
than immediately — that is the mapping working, not a dead source.

Nothing is added to `news_tracked_asset`. That registry is the allow-list for
the keyword-matched trade-press tier and the GDELT fallback, and
docs/news-sources.md §9 puts it third in a strict preference order — reach
for it "only if neither [a feed nor a Snapshot space] exists", and never for
a short ticker (`ETH` is the doc's own example of what must not go there).
Every symbol seeded here now has exact, mapping-based attribution, so adding
it to the allow-list as well would buy breadth by reintroducing exactly the
guessing that AUD-515 removed. Widening the press tier to more of the
portfolio is a deliberate loosening of that rule and belongs in its own
decision, not smuggled into a seed.

Idempotent throughout: re-running inserts nothing twice, and a `news_feed`
whose key already exists is left alone rather than duplicated.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision: str | None = "0030"
branch_labels = None
depends_on = None

# (key, tier, url, symbols)
_SEED_FEEDS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "project:kyberswap-blog",
        "project",
        "https://blog.kyberswap.com/feed/",
        ("KNC",),
    ),
    (
        "release:kyberswap-interface",
        "release",
        "https://github.com/KyberNetwork/kyberswap-interface/releases.atom",
        ("KNC",),
    ),
    (
        "release:ens-contracts",
        "release",
        "https://github.com/ensdomains/ens-contracts/releases.atom",
        ("ENS",),
    ),
    (
        "project:lido-blog",
        "project",
        "https://blog.lido.fi/rss/",
        ("LDO",),
    ),
    (
        "project:curve-news",
        "project",
        "https://news.curve.finance/feed/",
        ("CRV",),
    ),
    (
        "release:polygon-bor",
        "release",
        "https://github.com/maticnetwork/bor/releases.atom",
        ("MATIC",),
    ),
]

# (asset_symbol, space_id)
_SEED_SPACES: list[tuple[str, str]] = [
    ("ENS", "ens.eth"),
    ("LDO", "lido-snapshot.eth"),
    ("SUSHI", "sushigov.eth"),
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
            # The 0029/0030 trigger on this INSERT clears the feed's
            # conditional-GET validators and poll due-time, so a symbol added
            # to an already-polled feed takes effect on the next run.
            conn.execute(
                sa.text(
                    "INSERT INTO news_feed_symbol (feed_id, asset_symbol) "
                    "VALUES (:feed_id, :symbol) ON CONFLICT DO NOTHING"
                ),
                {"feed_id": feed_id, "symbol": symbol},
            )

    for asset_symbol, space_id in _SEED_SPACES:
        conn.execute(
            sa.text(
                "INSERT INTO snapshot_space (id, asset_symbol, space_id) "
                "SELECT :id, :asset_symbol, :space_id WHERE NOT EXISTS ("
                "  SELECT 1 FROM snapshot_space"
                "  WHERE asset_symbol = :asset_symbol AND space_id = :space_id"
                ")"
            ),
            {"id": uuid.uuid4(), "asset_symbol": asset_symbol, "space_id": space_id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    keys = [key for key, _tier, _url, _symbols in _SEED_FEEDS]
    conn.execute(
        sa.text("DELETE FROM news_feed WHERE key = ANY(:keys)"),
        {"keys": keys},
    )

    for asset_symbol, space_id in _SEED_SPACES:
        conn.execute(
            sa.text(
                "DELETE FROM snapshot_space "
                "WHERE asset_symbol = :asset_symbol AND space_id = :space_id"
            ),
            {"asset_symbol": asset_symbol, "space_id": space_id},
        )
