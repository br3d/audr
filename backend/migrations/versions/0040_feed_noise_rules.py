"""Feed-level keep/block noise rules, data instead of code (AUD-655, AUD-644).

`jobs/news.py` grew a noise rule per incident: `is_noisy_release()` (AUD-640,
package tags and pre-release builds on the `release` tier), the opt-in
`_RELEASE_SHAPED_PROJECT_FEEDS` set (AUD-640 reopened, the pre-release half
applied to one named project feed), and `is_noisy_aggregator_title()`
(AUD-654, evergreen explainers on the `aggregator` tier). Each one is a
code change + migration + deploy for a problem that is about one feed's
data, not the application's logic — this is the Miniflux `keep`/`block`
mechanism the founder asked to port (not the tool itself).

Two nullable columns, checked at ingestion in that order (`jobs/news.py`):
a `keep_pattern` is an allow-list — a title that fails it is dropped before
`block_pattern` is even consulted; a `block_pattern` drops a title it
matches. Both are plain Python regex text, compiled with `re.IGNORECASE` by
the caller — operators do not need to think about `(?i)` inline flags.

This migration also moves the two existing hardcodes into data:

* `project:polygon-forum-announcements` (0038) gets the prerelease half of
  `_RELEASE_SHAPED_PROJECT_FEEDS`'s filter as `block_pattern`. The package-tag
  half never applied to this feed (a forum post is not a package tag), so
  only the prerelease pattern moves.
* Every `aggregator:*` feed (0039) gets `is_noisy_aggregator_title()`'s
  pattern as `block_pattern` — expanded, not copied verbatim. Measured
  against 1836 live rows on the `aggregator` tier (AUD-655 wake comment),
  the old pattern missed two noise families outright:

    - Quote/converter spam, 472 of 1836 rows (26%): `Convert 0.1 PENDLE
      (Pendle) to USD ... - Bybit`, `Price of Decentraland (MANA) today in
      EUR ... - Bit2Me`. A quoted-name Google News search returns these
      honestly — the project name is in the title — so the relevance gate
      (`match_assets`) is not the layer that can drop them.
    - `Price Forecast`, 6 rows: the old pattern's `price predictions?` never
      covered `forecast`. `ETH Price Forecast: Ethereum reclaims $2,500 as
      stocks rally - FXStreet` is accepted as noise by this rule even though
      it names a real event — FXStreet's forecast stream reads as daily
      analysis, not news, and the pattern is a title-shape filter, not a
      content judgement; a sharper rule would need to parse the headline.

`is_noisy_release()` (the `release`-tier backstop) is **not** moved to data
and stays hardcoded in `jobs/news.py` — see its docstring there for why: a
release feed re-enabled or newly seeded must not quietly reintroduce package
tags or pre-release builds just because nobody remembered to set its
`block_pattern`. Every other noise rule here was opt-in by key or by tier
membership already, so moving it to a column changes nothing about who has
to remember to configure it.

A pattern that fails to compile (`re.error`) does not fail the run — the
feed polls as if the pattern were unset, logged via `_compile_feed_pattern`
in `jobs/news.py`, counted in the `news_refresh` run summary as
`pattern_errors`.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision: str | None = "0039"
branch_labels = None
depends_on = None

# Mirrors _PRERELEASE_RE in jobs/news.py (AUD-640) — only the prerelease half
# ever applied to this feed; `_PACKAGE_TAG_RE` is release-tier only and this
# feed's tier is `project`.
_POLYGON_FORUM_BLOCK_PATTERN = (
    r"(?<![a-z])(?:alpha|beta|rc|nightly|canary|preconf|pre-?release|unstable)(?![a-z])"
    r"|testing only"
)

# Supersedes _EVERGREEN_EXPLAINER_RE (AUD-654). Added here, not in the old
# constant: `forecasts?` alongside `predictions?` (the FXStreet/Phemex/
# CoinGape gap), and the two quote-spam shapes measured live (AUD-655 wake
# comment) — `Convert <amount> <TICKER> ...` and `Price of <name> today in
# <FIAT> ...`.
_AGGREGATOR_BLOCK_PATTERN = (
    r"^(?:what\s+is|what'?s|how\s+(?:does|to)|beginner'?s\s+guide|complete\s+guide)\b"
    r"|\bprice\s+(?:predictions?|forecasts?)\b"
    r"|\(20\d{2}\)"
    r"|^convert\s+[\d.,]+\s+[a-z]+\b"
    r"|^price\s+of\s+.+?\s+today\s+in\s+[a-z]+\b"
)


def upgrade() -> None:
    op.add_column("news_feed", sa.Column("keep_pattern", sa.Text(), nullable=True))
    op.add_column("news_feed", sa.Column("block_pattern", sa.Text(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE news_feed SET block_pattern = :pattern"
            " WHERE key = 'project:polygon-forum-announcements'"
        ),
        {"pattern": _POLYGON_FORUM_BLOCK_PATTERN},
    )
    conn.execute(
        sa.text("UPDATE news_feed SET block_pattern = :pattern WHERE tier = 'aggregator'"),
        {"pattern": _AGGREGATOR_BLOCK_PATTERN},
    )


def downgrade() -> None:
    op.drop_column("news_feed", "block_pattern")
    op.drop_column("news_feed", "keep_pattern")
