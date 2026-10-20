"""Retire the two noise-only release feeds and purge stored noise (AUD-640).

The founder reported the ENS card filling with `@ensdomains/ensjs@5.2.4`-style
rows. Read against the live data, the release tier splits cleanly in two:

* **Protocol software** — `release:go-ethereum`, `release:polygon-bor`,
  `release:ens-contracts`, `release:uniswap-v3-core`, `release:aave-v3-core`,
  `release:chainlink`. A node or contract release is news to a holder. These
  stay; the pre-release filter in `jobs/news.py` (`is_noisy_release`) strips
  their beta/testing tags, which was 8 of `polygon-bor`'s 10 rows.
* **Developer artefacts** — `release:ensjs` (14 rows, every one an npm
  package tag across four packages of one monorepo) and
  `release:kyberswap-interface` (11 rows, every one a front-end app build
  `KyberSwap Interface - v7.11.x`). No entry on either feed has ever been
  portfolio news, so a title filter would leave two permanently empty polls.
  Disabled here instead.

Coverage after this (docs/news-sources.md §9): ENS keeps `project:ens-blog`
(publishing, newest 2026-10-06), `release:ens-contracts` and Snapshot
`ens.eth`; KNC — the largest holding — keeps `project:kyberswap-blog`
(publishing, newest 2026-10-16). Neither asset loses its last source, which
is the bar for disabling a feed rather than filtering it.

Rows already ingested are deleted, not left to age out over 90 days: the
report was about what the cards show now. `news_alert.asset_news_id` is
ON DELETE CASCADE (0033), so any alert raised off a purged row goes with it.

Feeds are disabled rather than deleted so the row keeps its history and a
later decision to re-enable is one UPDATE, and so `downgrade` is exact.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision: str | None = "0036"
branch_labels = None
depends_on = None

_DISABLED_FEEDS = ["release:ensjs", "release:kyberswap-interface"]

# Mirrors is_noisy_release() in jobs/news.py for the rows that predate it.
# Bound as parameters rather than inlined so the patterns read the same way
# they do in Python and the `release:%` prefix stays the only literal.
_PACKAGE_TAG_PATTERN = r"@v?[0-9]+\.[0-9]+"
_PRERELEASE_PATTERN = (
    r"(^|[^a-z])(alpha|beta|rc|nightly|canary|preconf|pre-?release|unstable)([^a-z]|$)"
    r"|testing only"
)
_NOISE_SQL = """
    DELETE FROM asset_news
    WHERE source LIKE 'release:%'
      AND (title ~ :package_tag OR title ~* :prerelease)
"""


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE news_feed SET enabled = false WHERE key = ANY(:keys)"),
        {"keys": _DISABLED_FEEDS},
    )
    conn.execute(
        sa.text("DELETE FROM asset_news WHERE source = ANY(:keys)"),
        {"keys": _DISABLED_FEEDS},
    )
    conn.execute(
        sa.text(_NOISE_SQL),
        {"package_tag": _PACKAGE_TAG_PATTERN, "prerelease": _PRERELEASE_PATTERN},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE news_feed SET enabled = true WHERE key = ANY(:keys)"),
        {"keys": _DISABLED_FEEDS},
    )
