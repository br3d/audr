"""Keyless breadth tier: Hacker News (Algolia) per held symbol (AUD-656, AUD-644).

The founder's call on AUD-644/AUD-656 was Hacker News only, not Reddit or
public Telegram channels — HN has moderation and the audience is
technical, neither of which holds for the other two. `hn.algolia.com`
(`/api/v1/search_by_date`) is the keyless, no-account search API HN itself
publishes against its own Algolia index; `hnrss.org` was considered and
rejected because it is a third-party proxy in front of that same API —
depending on it adds a second point of failure for zero extra coverage.

`hackernews` is a new `news_feed` tier, one row per held symbol, generated
here rather than hand-curated like `project`/`release` — same reasoning as
the `aggregator` tier (0039): the point is that it needs no per-asset
research. The query is templated from `asset.name` (the project name, not
the ticker — a three-letter symbol like `ENS`, `UNI` or `CRV` is mostly
noise on HN's search index), quoted:

    query = "<name>", tags=story, restrictSearchableAttributes=title

Quoting and restricting to the title attribute both measurably cut HN's
typo-tolerant search noise (AUD-656 wake comment: `"Aave"` + title
restriction returned real Aave coverage where an unquoted, unrestricted
query returned stories with no relation to the word at all) — the same
quoted-phrase trick `GdeltProvider.search_news` and the Google News
aggregator tier already use.

Ingestion lives in `jobs/news.py`, not the generic `_poll_feed`/`parse_feed`
RSS path this registry otherwise drives: Algolia returns JSON, not
Atom/RSS, and the query carries its own per-run budget discipline
(`_HN_MAX_QUERIES_PER_RUN`, mirroring `_GDELT_MAX_QUERIES_PER_RUN`) rather
than the unconstrained per-feed fan-out `_poll_feed` uses for every other
tier — Algolia is anonymous and, while it tolerated a quick burst of
sequential calls when measured live, publishes no documented rate limit or
SLA either, the same posture GDELT's throttling already assumes. A
`hackernews:*` feed row still carries the registry's ordinary health
columns (`last_polled_at`, `consecutive_failures`, `last_error`) and its
`keep_pattern`/`block_pattern` columns (AUD-655) for noise an operator finds
live and wants to silence per project, without a code change — a project
name alone cannot resolve e.g. "Polygon" colliding with the Polygon.com
gaming site or ordinary geometry articles; that is exactly the kind of
feed-specific noise this mechanism exists for.

`covered_asset_symbols()` now also counts `hackernews` alongside
`project`/`release`/`aggregator`: a symbol with its own HN feed does not
need to also burn one of GDELT's scarce `_GDELT_MAX_QUERIES_PER_RUN` slots.

Only currently-held symbols are seeded — same limitation every other
generated tier has (0039's docstring explains why): an asset acquired after
this migration runs gets no `hackernews` row until a later migration (or a
manual INSERT) adds one. A fresh database with no balances yet seeds zero
rows here, which is correct, not a bug.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision: str | None = "0040"
branch_labels = None
depends_on = None

_TIER = "hackernews"
_ALL_TIERS = ("project", "release", "press", "aggregator", "hackernews")


def _hackernews_url(name: str) -> str:
    encoded_query = quote(f'"{name}"', safe="")
    return (
        "https://hn.algolia.com/api/v1/search_by_date"
        f"?query={encoded_query}&tags=story&restrictSearchableAttributes=title"
    )


def _drop_tier_check() -> None:
    """Drop the `news_feed` tier CHECK by its real name, whichever it is.

    Same trap 0039/0034 already document: 0024's `create_table` doubled the
    constraint name to `ck_news_feed_ck_news_feed_tier`; 0039's
    `create_check_constraint("tier", ...)` rewrote it to the undoubled
    `ck_news_feed_tier`. Matching on the constraint's definition rather than
    either literal spelling keeps `downgrade()` correct from either
    starting shape.
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
                {"id": feed_id, "key": key, "tier": _TIER, "url": _hackernews_url(name)},
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
    conn.execute(sa.text("DELETE FROM asset_news WHERE source = :source"), {"source": "hackernews"})
    conn.execute(sa.text("DELETE FROM news_feed WHERE tier = :tier"), {"tier": _TIER})

    _drop_tier_check()
    op.create_check_constraint(
        "tier",
        "news_feed",
        "tier = ANY (ARRAY['project'::text, 'release'::text, 'press'::text, 'aggregator'::text])",
    )
