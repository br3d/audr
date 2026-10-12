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
    asset_symbol: str | None
    url: str
    etag: str | None
    last_modified: str | None


async def list_due_feeds(session: AsyncSession) -> list[NewsFeed]:
    """Return enabled feeds not polled within MIN_POLL_INTERVAL."""
    result = await session.execute(
        sa.text(
            """
            SELECT id, key, tier, asset_symbol, url, etag, last_modified
            FROM news_feed
            WHERE enabled
              AND (last_polled_at IS NULL OR last_polled_at < :cutoff)
            ORDER BY key
            """
        ),
        {"cutoff": datetime.now(tz=UTC) - MIN_POLL_INTERVAL},
    )
    return [
        NewsFeed(
            id=row[0],
            key=row[1],
            tier=row[2],
            asset_symbol=row[3],
            url=row[4],
            etag=row[5],
            last_modified=row[6],
        )
        for row in result
    ]


async def covered_asset_symbols(session: AsyncSession) -> set[str]:
    """Return asset symbols with an enabled project/release feed.

    Used to decide which held assets fall back to the GDELT breadth tier —
    an asset with its own native feed does not need a general-news query.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT asset_symbol FROM news_feed
            WHERE enabled AND tier IN ('project', 'release') AND asset_symbol IS NOT NULL
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
