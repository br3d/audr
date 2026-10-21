"""DB access for the keyless news feed registry (AUD-512).

Per-feed health and conditional-GET validators live on `news_feed`
(migration 0024). Throttling is per-feed (`last_polled_at`) rather than the
single global check `news_refresh` used against the retired CoinGecko path
— one broken or slow feed must not delay every other feed's poll.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

MIN_POLL_INTERVAL = timedelta(minutes=15)

# Error text is operator-facing (surfaced via the status/job API), not a
# secret, but still bounded so a pathological response body can't bloat the
# row.
_MAX_ERROR_LEN = 500


@dataclass(frozen=True)
class NewsFeed:
    id: uuid.UUID
    key: str
    tier: str
    symbols: frozenset[str]
    url: str
    etag: str | None
    last_modified: str | None


async def list_due_feeds(session: AsyncSession) -> list[NewsFeed]:
    """Return enabled feeds not polled within MIN_POLL_INTERVAL.

    A feed's symbols come from `news_feed_symbol` (AUD-571) — zero rows
    means a press-tier feed matched by allow-list keyword, not a feed
    nobody has mapped yet.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT nf.id, nf.key, nf.tier, nf.url, nf.etag, nf.last_modified,
                   COALESCE(
                       array_agg(nfs.asset_symbol) FILTER (WHERE nfs.asset_symbol IS NOT NULL),
                       ARRAY[]::text[]
                   ) AS symbols
            FROM news_feed nf
            LEFT JOIN news_feed_symbol nfs ON nfs.feed_id = nf.id
            WHERE nf.enabled
              AND (nf.last_polled_at IS NULL OR nf.last_polled_at < :cutoff)
            GROUP BY nf.id, nf.key, nf.tier, nf.url, nf.etag, nf.last_modified
            ORDER BY nf.key
            """
        ),
        {"cutoff": datetime.now(tz=UTC) - MIN_POLL_INTERVAL},
    )
    return [
        NewsFeed(
            id=row[0],
            key=row[1],
            tier=row[2],
            symbols=frozenset(symbol.upper() for symbol in row[6]),
            url=row[3],
            etag=row[4],
            last_modified=row[5],
        )
        for row in result
    ]


async def covered_asset_symbols(session: AsyncSession) -> set[str]:
    """Return asset symbols with an enabled project/release/aggregator feed.

    Used to decide which held assets fall back to the GDELT breadth tier —
    an asset with its own native feed, or a per-symbol aggregator feed
    (AUD-654), does not need a GDELT query too; GDELT's scarce query budget
    (`_GDELT_MAX_QUERIES_PER_RUN`) stays for symbols neither tier reaches.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT nfs.asset_symbol
            FROM news_feed_symbol nfs
            JOIN news_feed nf ON nf.id = nfs.feed_id
            WHERE nf.enabled AND nf.tier IN ('project', 'release', 'aggregator')
            """
        )
    )
    return {row[0] for row in result}


async def record_feed_not_modified(session: AsyncSession, *, feed_id: uuid.UUID) -> None:
    """304 Not Modified is success — reset failures, advance the poll clock."""
    await session.execute(
        sa.text(
            """
            UPDATE news_feed
            SET last_polled_at = now(), last_success_at = now(),
                consecutive_failures = 0, last_error = NULL
            WHERE id = :id
            """
        ),
        {"id": feed_id},
    )


async def record_feed_success(
    session: AsyncSession,
    *,
    feed_id: uuid.UUID,
    etag: str | None,
    last_modified: str | None,
) -> None:
    await session.execute(
        sa.text(
            """
            UPDATE news_feed
            SET last_polled_at = now(), last_success_at = now(),
                consecutive_failures = 0, last_error = NULL,
                etag = :etag, last_modified = :last_modified
            WHERE id = :id
            """
        ),
        {"id": feed_id, "etag": etag, "last_modified": last_modified},
    )


async def record_feed_failure(session: AsyncSession, *, feed_id: uuid.UUID, error: str) -> None:
    """Increment the failure streak without touching last_success_at.

    Never raises on its own — a feed that 404s or returns HTML must be
    visibly stale (AUD-512 req. 5), not break the run for every other feed.
    """
    await session.execute(
        sa.text(
            """
            UPDATE news_feed
            SET last_polled_at = now(),
                consecutive_failures = consecutive_failures + 1,
                last_error = :error
            WHERE id = :id
            """
        ),
        {"id": feed_id, "error": error[:_MAX_ERROR_LEN]},
    )
