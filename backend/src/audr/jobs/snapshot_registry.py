"""DB access for the Snapshot governance space registry (AUD-513).

`snapshot_space` maps a held asset's symbol to the Snapshot Hub space that
governs it — explicit configuration, not a symbol/keyword guess (the
failure mode docs/news-sources.md §3 calls out: a headline containing the
word "uni" must never be treated as news about the UNI holding). Health
counters mirror `news_feed_registry.py`'s pattern so a dead or renamed
space is visibly stale rather than silently dropping coverage.
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
class SnapshotSpace:
    id: uuid.UUID
    asset_symbol: str
    space_id: str


async def list_due_spaces(session: AsyncSession) -> list[SnapshotSpace]:
    """Return enabled spaces not polled within MIN_POLL_INTERVAL."""
    result = await session.execute(
        sa.text(
            """
            SELECT id, asset_symbol, space_id
            FROM snapshot_space
            WHERE enabled
              AND (last_polled_at IS NULL OR last_polled_at < :cutoff)
            ORDER BY asset_symbol
            """
        ),
        {"cutoff": datetime.now(tz=UTC) - MIN_POLL_INTERVAL},
    )
    return [SnapshotSpace(id=row[0], asset_symbol=row[1], space_id=row[2]) for row in result]


async def record_space_success(session: AsyncSession, *, space_id: uuid.UUID) -> None:
    await session.execute(
        sa.text(
            """
            UPDATE snapshot_space
            SET last_polled_at = now(), last_success_at = now(),
                consecutive_failures = 0, last_error = NULL
            WHERE id = :id
            """
        ),
        {"id": space_id},
    )


async def record_space_failure(session: AsyncSession, *, space_id: uuid.UUID, error: str) -> None:
    """Increment the failure streak without touching last_success_at.

    Never raises on its own — a space whose fetch fails must be visibly
    stale (AUD-513 req. 4), not break the run for every other source.
    """
    await session.execute(
        sa.text(
            """
            UPDATE snapshot_space
            SET last_polled_at = now(),
                consecutive_failures = consecutive_failures + 1,
                last_error = :error
            WHERE id = :id
            """
        ),
        {"id": space_id, "error": error[:_MAX_ERROR_LEN]},
    )
