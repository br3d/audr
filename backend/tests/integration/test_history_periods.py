"""Integration tests for the 90d/1y history periods (AUD-376).

Extends query_history() coverage for the two periods added on top of the
existing 24h/7d/30d/all set: window boundary correctness, thinning of dense
long-range history, and the empty/short-range edge cases.

Lives in its own module (rather than test_history.py) so it carries the
`integration` marker and is picked up by `./scripts/test.sh --backend-only`'s
marker-filtered run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.history_query import MAX_POINTS, query_history

pytestmark = pytest.mark.integration


async def _bulk_insert_history_points(
    session: AsyncSession,
    timestamps: list[datetime],
) -> list[uuid.UUID]:
    """Directly insert valuation_snapshot + history_point rows for each
    timestamp, bypassing balance_observation/valuation_line — query_history
    only ever reads from history_point, so this is enough and much faster
    than the full chain for tests that need many points."""
    ids = [uuid.uuid4() for _ in timestamps]
    snap_rows = [
        {"id": str(sid), "ts": ts, "input_key": str(sid)}
        for sid, ts in zip(ids, timestamps, strict=True)
    ]
    hp_rows = [
        {"id": str(uuid.uuid4()), "sid": str(sid), "ts": ts}
        for sid, ts in zip(ids, timestamps, strict=True)
    ]
    await session.execute(
        sa.text(
            "INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at, input_key)"
            " VALUES (:id, :ts, 'complete', :ts, :input_key)"
        ),
        snap_rows,
    )
    await session.execute(
        sa.text(
            """
            INSERT INTO history_point
              (id, snapshot_id, snapshotted_at, total_value_usd, quality,
               included_wallet_count, included_asset_count, has_gap, is_canonical)
            VALUES (:id, :sid, :ts, '100.0', 'complete', 1, 1, false, true)
            """
        ),
        hp_rows,
    )
    return ids


async def test_period_90d_excludes_older_points(db_session: AsyncSession) -> None:
    """period='90d' excludes points older than 90 days."""
    now = datetime.now(tz=UTC)
    recent_ts = now - timedelta(days=10)
    old_ts = now - timedelta(days=100)

    ids = await _bulk_insert_history_points(db_session, [recent_ts, old_ts])

    page = await query_history(db_session, period="90d")
    real_entries = [e for e in page.entries if not e.is_gap_marker]
    assert len(real_entries) == 1
    assert real_entries[0].snapshot_id == ids[0]


async def test_period_1y_excludes_older_points(db_session: AsyncSession) -> None:
    """period='1y' excludes points older than 365 days."""
    now = datetime.now(tz=UTC)
    recent_ts = now - timedelta(days=100)
    old_ts = now - timedelta(days=400)

    ids = await _bulk_insert_history_points(db_session, [recent_ts, old_ts])

    page = await query_history(db_session, period="1y")
    real_entries = [e for e in page.entries if not e.is_gap_marker]
    assert len(real_entries) == 1
    assert real_entries[0].snapshot_id == ids[0]


async def test_period_90d_and_1y_match_all_for_small_recent_dataset(
    db_session: AsyncSession,
) -> None:
    """When the whole history fits comfortably under the thinning budget
    (today's reality: a single day of canonical points), period='90d' and
    period='1y' must return exactly the same set as period='all' — this is
    the expected, documented behaviour until history accumulates (AUD-376)."""
    base = datetime.now(tz=UTC) - timedelta(hours=9)
    timestamps = [base + timedelta(hours=i) for i in range(10)]
    ids = await _bulk_insert_history_points(db_session, timestamps)

    page_all = await query_history(db_session, period="all")
    page_90d = await query_history(db_session, period="90d")
    page_1y = await query_history(db_session, period="1y")

    ids_all = {e.snapshot_id for e in page_all.entries if not e.is_gap_marker}
    ids_90d = {e.snapshot_id for e in page_90d.entries if not e.is_gap_marker}
    ids_1y = {e.snapshot_id for e in page_1y.entries if not e.is_gap_marker}

    assert ids_all == set(ids)
    assert ids_90d == ids_all
    assert ids_1y == ids_all


async def test_period_90d_empty_when_no_points(db_session: AsyncSession) -> None:
    """period='90d' returns an empty page (not an error) when there is no history."""
    page = await query_history(db_session, period="90d")
    assert page.entries == []
    assert page.next_cursor is None


async def test_period_1y_short_range_returns_available_points(
    db_session: AsyncSession,
) -> None:
    """period='1y' returns the single available point for a short, recent range."""
    ts = datetime.now(tz=UTC) - timedelta(hours=2)
    ids = await _bulk_insert_history_points(db_session, [ts])

    page = await query_history(db_session, period="1y")
    real_entries = [e for e in page.entries if not e.is_gap_marker]
    assert len(real_entries) == 1
    assert real_entries[0].snapshot_id == ids[0]


async def test_period_1y_thins_dense_long_history(db_session: AsyncSession) -> None:
    """A dense history (hourly-ish cadence over many weeks) must not be
    returned point-for-point on period='1y' — the response would balloon.
    period='all' stays raw/unthinned; 'all' and '1y' cover the same window
    here, so the thinned '1y' response must be strictly smaller than 'all'."""
    base = datetime.now(tz=UTC) - timedelta(days=50)
    timestamps = [base + timedelta(hours=2 * i) for i in range(600)]
    await _bulk_insert_history_points(db_session, timestamps)

    page_all = await query_history(db_session, period="all", limit=MAX_POINTS)
    page_1y = await query_history(db_session, period="1y", limit=MAX_POINTS)

    real_all = [e for e in page_all.entries if not e.is_gap_marker]
    real_1y = [e for e in page_1y.entries if not e.is_gap_marker]

    assert len(real_all) == 600
    assert 0 < len(real_1y) < len(real_all)
    # every returned point must still fall within the 1y window
    cutoff = datetime.now(tz=UTC) - timedelta(days=365)
    assert all(e.snapshotted_at >= cutoff for e in real_1y)


async def test_period_90d_thins_dense_long_history(db_session: AsyncSession) -> None:
    """Same thinning expectation as the 1y case, but for the 90d window."""
    base = datetime.now(tz=UTC) - timedelta(days=50)
    timestamps = [base + timedelta(hours=2 * i) for i in range(600)]
    await _bulk_insert_history_points(db_session, timestamps)

    page_all = await query_history(db_session, period="all", limit=MAX_POINTS)
    page_90d = await query_history(db_session, period="90d", limit=MAX_POINTS)

    real_all = [e for e in page_all.entries if not e.is_gap_marker]
    real_90d = [e for e in page_90d.entries if not e.is_gap_marker]

    assert len(real_all) == 600
    assert 0 < len(real_90d) < len(real_all)
    cutoff = datetime.now(tz=UTC) - timedelta(days=90)
    assert all(e.snapshotted_at >= cutoff for e in real_90d)
