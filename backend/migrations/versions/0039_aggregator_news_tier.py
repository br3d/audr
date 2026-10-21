"""Keyless breadth tier: Google News RSS per held symbol (AUD-654, AUD-644).

The founder's call on AUD-644 was to prioritise *coverage* — a project feed
exists for 9 of ~70 held assets — over building a second collector, since
the only other keyless breadth lever (GDELT) 429s on its first anonymous
request and Reddit follows after a couple of calls. Checked live on
2026-10-21: `news.google.com/rss/search?q=...` returns HTTP 200 and up to 100
entries per symbol with **no key**, for assets that have no first-party feed
at all (SUSHI's forum moved off Discourse and its Medium is empty; Pendle has
none).

`aggregator` is a new `news_feed` tier, one row per held symbol, generated
here rather than hand-curated like `project`/`release`: the point of this
tier is that it needs no per-asset research, so seeding it the way
0024/0031/0038 seed their rows (research each URL, type it into the
migration) would defeat the reason it exists. The query is templated from
`asset.name`, quoted, plus a `crypto` qualifier to keep a three-letter ticker
like ENS or CRV from pulling in unrelated headlines:

    https://news.google.com/rss/search?q=%22<name>%22+crypto&hl=en-US&gl=US&ceid=US:en

Ingestion is the existing conditional-GET + `parse_feed` + dedup path
(`jobs/news.py`) — Google News RSS is a standard RSS 2.0 document, nothing
new to parse. Two things are specific to this tier, both in `jobs/news.py`
rather than here:

* **Relevance gate** — a quoted-phrase search is not attribution-exact the
  way a project blog is, so `_targets_for_feed` runs the press tier's
  `match_assets` (symbol-or-name, word-boundary) against this feed's own
  asset before accepting an item, instead of trusting `feed.symbols` alone.
* **Evergreen/SEO noise** — the newest row Google News served for SUSHI on
  2026-10-21 was "What Is SushiSwap and How Does It Work? — Binance", a
  perennial explainer, not news. `is_noisy_aggregator_title()` drops that
  shape at ingestion, same backstop-constant approach as
  `is_noisy_release()` (AUD-640) — no data-driven registry yet.

`covered_asset_symbols()` (news_feed_registry.py) now counts `aggregator`
alongside `project`/`release`: a symbol seeded here no longer needs the
GDELT fallback, and GDELT's scarce `_GDELT_MAX_QUERIES_PER_RUN` slots stay
free for whatever this migration did not reach (an asset with no
`balance_observation` yet, e.g. a fresh install).

Only currently-held symbols are seeded — same limitation project/release
coverage already has (docs/news-sources.md "Adding a new holding to news
coverage"): an asset acquired after this migration runs gets no aggregator
row until a later migration (or a manual INSERT) adds one. A fresh database
with no balances yet seeds zero rows here, which is correct, not a bug.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision: str | None = "0038"
branch_labels = None
depends_on = None

_TIER = "aggregator"
_ALL_TIERS = ("project", "release", "press", "aggregator")


def _aggregator_url(name: str) -> str:
    encoded_name = quote(name, safe="")
    return f"https://news.google.com/rss/search?q=%22{encoded_name}%22+crypto&hl=en-US&gl=US&ceid=US:en"


def _drop_tier_check() -> None:
    """Drop the `news_feed` tier CHECK by its real name, whichever it is.

    This constraint has two names in the wild and a literal is wrong on one of
    them — the same trap 0034 documents (AUD-597):

      created by 0024's `create_table`   ck_news_feed_ck_news_feed_tier
      rewritten by `create_check_constraint("tier", ...)`  ck_news_feed_tier

    0024 declared it as `sa.CheckConstraint(name="ck_news_feed_tier")` inside
    `op.create_table`, where `ck_%(table_name)s_%(constraint_name)s` doubled
    the spelling; that is the shape on the stand (verified at revision 0038).
    But `upgrade()` below re-creates it from the bare name `tier`, so after
    0039 the very same database carries the *undoubled* spelling. A
    `downgrade()` that spelled either literal would therefore work on one side
    of the migration and raise `UndefinedObject` on the other — and a
    downgrade is wanted exactly when a deploy has migrated and then failed
    (deploy-runbook.md, "A rollback must restore the schema").

    So match on what both shapes have in common, the constraint definition.
    """
    bind = op.get_bind()
    names = (
        bind.execute(
            sa.text(
                "SELECT c.conname FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid"
                " WHERE t.relname = 'news_feed' AND c.contype = 'c'"
                "   AND pg_get_constraintdef(c.oid) LIKE '%tier%'"
            )
        )
        .scalars()
        .all()
    )
    for name in names:
        # Identifier from pg_constraint, not from input; quoted all the same.
        op.execute(f'ALTER TABLE news_feed DROP CONSTRAINT "{name}"')  # noqa: S608


def upgrade() -> None:
    conn = op.get_bind()

    _drop_tier_check()
    op.create_check_constraint(
        "tier",
        "news_feed",
        "tier = ANY (ARRAY[" + ", ".join(f"'{tier}'::text" for tier in _ALL_TIERS) + "])",
    )

    held = conn.execute(
        sa.text(
            """
            SELECT DISTINCT ON (a.symbol) a.symbol, a.name
            FROM balance_observation bo
            JOIN asset a ON a.id = bo.asset_id
            WHERE bo.raw_amount > 0
            ORDER BY a.symbol, a.name
            """
        )
    ).all()

    for symbol, name in held:
        key = f"{_TIER}:{symbol.lower()}"
        feed_id = conn.scalar(sa.text("SELECT id FROM news_feed WHERE key = :key"), {"key": key})
        if feed_id is None:
            feed_id = uuid.uuid4()
            conn.execute(
                sa.text(
                    "INSERT INTO news_feed (id, key, tier, url) VALUES (:id, :key, :tier, :url)"
                ),
                {"id": feed_id, "key": key, "tier": _TIER, "url": _aggregator_url(name)},
            )
        conn.execute(
            sa.text(
                "INSERT INTO news_feed_symbol (feed_id, asset_symbol) "
                "VALUES (:feed_id, :symbol) ON CONFLICT DO NOTHING"
            ),
            {"feed_id": feed_id, "symbol": symbol.upper()},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM asset_news WHERE source LIKE :prefix"), {"prefix": "aggregator:%"}
    )
    conn.execute(sa.text("DELETE FROM news_feed WHERE tier = :tier"), {"tier": _TIER})

    _drop_tier_check()
    op.create_check_constraint(
        "tier",
        "news_feed",
        "tier = ANY (ARRAY['project'::text, 'release'::text, 'press'::text])",
    )
