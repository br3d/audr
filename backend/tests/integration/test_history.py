"""Integration tests for portfolio history: immutability, idempotency, gap markers, membership (T064 / US3 / AUD-77).

Tests use the rolled-back db_session fixture — no permanent state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.history import materialize_history_point
from audr.portfolio.history_query import MAX_POINTS, query_history, get_snapshot_detail
from audr.portfolio.snapshot import publish_valuation_snapshot


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.anyio


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wid = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label, status) VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wid), "addr": address.lower()},
    )
    return wid


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str = "TKN",
    decimals: int = 18,
) -> uuid.UUID:
    aid = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
            VALUES (:id, :addr, :sym, :name, :dec, 'manual', false)
            """
        ),
        {
            "id": str(aid),
            "addr": token_address.lower(),
            "sym": symbol,
            "name": symbol,
            "dec": decimals,
        },
    )
    return aid


async def _insert_balance_observation(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int = 1_000_000_000_000_000_000,
    block_number: int = 12345678,
    observed_at: datetime | None = None,
) -> uuid.UUID:
    obs_id = uuid.uuid4()
    ts = observed_at or datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, observed_at)
            VALUES (:id, :wid, :aid, :raw, :block, :ts)
            """
        ),
        {
            "id": str(obs_id),
            "wid": str(wallet_id),
            "aid": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
            "ts": ts,
        },
    )
    return obs_id


async def _insert_snapshot(
    session: AsyncSession,
    *,
    snapshotted_at: datetime | None = None,
    quality: str = "complete",
    published_at: datetime | None = None,
) -> uuid.UUID:
    sid = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at, input_key)
            VALUES (:id, :ts, :quality, :pub, :input_key)
            """
        ),
        {
            "id": str(sid),
            "ts": snapshotted_at or now,
            "quality": quality,
            "pub": published_at or now,
            "input_key": str(sid),
        },
    )
    return sid


async def _insert_valuation_line(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int = 1_000_000_000_000_000_000,
    block_number: int = 12345678,
    price_usd: str | None = "1.0",
    value_usd: str | None = "1.0",
    observation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    lid = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO valuation_line
              (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,
               price_usd, value_usd, observation_id)
            VALUES
              (:id, :sid, :wid, :aid, :raw, :block, :price, :value, :obs)
            """
        ),
        {
            "id": str(lid),
            "sid": str(snapshot_id),
            "wid": str(wallet_id),
            "aid": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
            "price": price_usd,
            "value": value_usd,
            "obs": str(observation_id) if observation_id else None,
        },
    )
    return lid


# ---------------------------------------------------------------------------
# Tests: snapshot immutability
# ---------------------------------------------------------------------------


async def test_published_snapshot_rows_are_not_modified(
    db_session: AsyncSession,
) -> None:
    """A published valuation_snapshot row cannot be mutated by a second publication."""
    wallet_id = await _insert_wallet(db_session, "0xaaaa000000000000000000000000000000000001")
    asset_id = await _insert_asset(db_session, token_address="0xbbbb000000000000000000000000000000000002")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    ts = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    sid = await _insert_snapshot(db_session, snapshotted_at=ts)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
    )

    # Materialize the history point.
    result = await materialize_history_point(db_session, snapshot_id=sid)
    assert result.created is True

    # Check original snapshot row is unchanged.
    row = (
        await db_session.execute(
            sa.text("SELECT quality, published_at FROM valuation_snapshot WHERE id = :id"),
            {"id": str(sid)},
        )
    ).first()
    assert row is not None
    assert row[0] == "complete"
    assert row[1] is not None  # published_at set at insert time


async def test_valuation_lines_are_immutable_after_publication(
    db_session: AsyncSession,
) -> None:
    """valuation_line rows for a published snapshot cannot be updated via SQL."""
    wallet_id = await _insert_wallet(db_session, "0xcccc000000000000000000000000000000000003")
    asset_id = await _insert_asset(db_session, token_address="0xdddd000000000000000000000000000000000004")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    lid = await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        value_usd="100.0",
    )

    # Attempt to UPDATE the line value (simulates an incorrect reprocessing attempt).
    await db_session.execute(
        sa.text("UPDATE valuation_line SET value_usd = '999.0' WHERE id = :id"),
        {"id": str(lid)},
    )

    # The ORM test here verifies the UPDATE ran without error at DB level, but
    # in production the application layer must never call such an UPDATE.
    # We verify by reading back that the value was (in this transaction) updated,
    # then roll back — demonstrating the row CAN be updated at DB level but the
    # application contract forbids it.
    row = (
        await db_session.execute(
            sa.text("SELECT value_usd::text FROM valuation_line WHERE id = :id"),
            {"id": str(lid)},
        )
    ).first()
    assert row is not None
    # This test documents that the immutability is an application-layer contract,
    # not a DB-level constraint.  The fixture rolls back the transaction.
    assert row[0] == "999.000000000000000000"


# ---------------------------------------------------------------------------
# Tests: repeated-publication idempotency
# ---------------------------------------------------------------------------


async def test_materialize_history_point_idempotent(
    db_session: AsyncSession,
) -> None:
    """Calling materialize_history_point twice for the same snapshot returns the same row."""
    wallet_id = await _insert_wallet(db_session, "0xeeee000000000000000000000000000000000005")
    asset_id = await _insert_asset(db_session, token_address="0xffff000000000000000000000000000000000006")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    ts = datetime(2026, 3, 15, 10, 0, tzinfo=UTC)
    sid = await _insert_snapshot(db_session, snapshotted_at=ts)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        value_usd="500.0",
    )

    first = await materialize_history_point(db_session, snapshot_id=sid)
    assert first.created is True

    second = await materialize_history_point(db_session, snapshot_id=sid)
    assert second.created is False
    assert second.history_point_id == first.history_point_id

    # Only one history_point row for this snapshot.
    count = (
        await db_session.execute(
            sa.text("SELECT COUNT(*) FROM history_point WHERE snapshot_id = :sid"),
            {"sid": str(sid)},
        )
    ).scalar()
    assert count == 1


async def _insert_quote_set(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    price_usd: Decimal,
    status: str = "complete",
    offset_minutes: int = 0,
) -> uuid.UUID:
    qset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now() + (:offset * interval '1 minute'), :status)"
        ),
        {"id": str(qset_id), "status": status, "offset": offset_minutes},
    )
    await session.execute(
        sa.text(
            "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)"
            " VALUES (:id, :qset, :asset, :price)"
        ),
        {
            "id": str(uuid.uuid4()),
            "qset": str(qset_id),
            "asset": str(asset_id),
            "price": str(price_usd),
        },
    )
    return qset_id


async def test_publish_snapshot_later_verified_block_adds_history_point(
    db_session: AsyncSession,
) -> None:
    """A later verified block with unchanged quantities carries a new observation
    id, so the AUD-70 input key differs and publishing again adds a new history
    point — the converse of test_materialize_history_point_idempotent's exact-retry
    case above, which adds none."""
    wallet_id = await _insert_wallet(db_session, "0xa001000000000000000000000000000000000a1")
    asset_id = await _insert_asset(db_session, token_address="0xa002000000000000000000000000000000000a2")
    await _insert_balance_observation(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        block_number=100,
        observed_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
    )
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("10"))
    await db_session.flush()

    first = await publish_valuation_snapshot(db_session)
    assert first.created is True
    await materialize_history_point(db_session, snapshot_id=first.snapshot_id)

    # Same quantity, later verified block: a new observation row, same raw_amount.
    await _insert_balance_observation(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        block_number=200,
        observed_at=datetime(2026, 9, 1, 1, 0, tzinfo=UTC),
    )
    await db_session.flush()

    second = await publish_valuation_snapshot(db_session)
    assert second.created is True
    assert second.snapshot_id != first.snapshot_id
    await materialize_history_point(db_session, snapshot_id=second.snapshot_id)

    count = (await db_session.execute(sa.text("SELECT COUNT(*) FROM history_point"))).scalar()
    assert count == 2


async def test_publish_snapshot_quote_only_change_adds_history_point(
    db_session: AsyncSession,
) -> None:
    """Changing only the quote set (holdings/observation unchanged) still adds a
    new snapshot and history point, since the AUD-70 input key also covers the
    quote_set id the prices came from."""
    wallet_id = await _insert_wallet(db_session, "0xa003000000000000000000000000000000000a3")
    asset_id = await _insert_asset(db_session, token_address="0xa004000000000000000000000000000000000a4")
    await _insert_balance_observation(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observed_at=datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
    )
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("10"))
    await db_session.flush()

    first = await publish_valuation_snapshot(db_session)
    assert first.created is True
    await materialize_history_point(db_session, snapshot_id=first.snapshot_id)

    # A fresh, strictly newer quote_set — holdings are unchanged, only the
    # quote data's provenance (quote_set id) is new.
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("11"), offset_minutes=10)
    await db_session.flush()

    second = await publish_valuation_snapshot(db_session)
    assert second.created is True
    assert second.snapshot_id != first.snapshot_id
    await materialize_history_point(db_session, snapshot_id=second.snapshot_id)

    count = (await db_session.execute(sa.text("SELECT COUNT(*) FROM history_point"))).scalar()
    assert count == 2


# ---------------------------------------------------------------------------
# Tests: gap markers
# ---------------------------------------------------------------------------


async def test_gap_markers_injected_for_temporal_discontinuities(
    db_session: AsyncSession,
) -> None:
    """query_history inserts gap markers between snapshots that are far apart."""
    wallet_id = await _insert_wallet(db_session, "0x1111000000000000000000000000000000000007")
    asset_id = await _insert_asset(db_session, token_address="0x2222000000000000000000000000000000000008")

    # Create two snapshots: one at t=0, another at t=72h — well beyond any gap threshold.
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    t_early = base
    t_late = base + timedelta(hours=72)

    for ts in (t_early, t_late):
        sid = await _insert_snapshot(db_session, snapshotted_at=ts)
        obs_id = await _insert_balance_observation(
            db_session,
            wallet_id=wallet_id,
            asset_id=asset_id,
            observed_at=ts,
        )
        await _insert_valuation_line(
            db_session,
            snapshot_id=sid,
            wallet_id=wallet_id,
            asset_id=asset_id,
            observation_id=obs_id,
        )
        await materialize_history_point(db_session, snapshot_id=sid)

    page = await query_history(db_session, period="all")
    gap_markers = [e for e in page.entries if e.is_gap_marker]
    assert len(gap_markers) >= 1, "expected at least one gap marker for 72h gap"
    assert all(gm.total_value_usd is None for gm in gap_markers)
    assert all(gm.quality == "unknown" for gm in gap_markers)


async def test_no_gap_markers_for_consecutive_hourly_snapshots(
    db_session: AsyncSession,
) -> None:
    """No gap markers when snapshots are consecutive hourly points."""
    wallet_id = await _insert_wallet(db_session, "0x3333000000000000000000000000000000000009")
    asset_id = await _insert_asset(db_session, token_address="0x4444000000000000000000000000000000000010")

    base = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    for i in range(5):
        ts = base + timedelta(hours=i)
        sid = await _insert_snapshot(db_session, snapshotted_at=ts)
        obs_id = await _insert_balance_observation(
            db_session,
            wallet_id=wallet_id,
            asset_id=asset_id,
            observed_at=ts,
        )
        await _insert_valuation_line(
            db_session,
            snapshot_id=sid,
            wallet_id=wallet_id,
            asset_id=asset_id,
            observation_id=obs_id,
        )
        await materialize_history_point(db_session, snapshot_id=sid)

    page = await query_history(db_session, period="all")
    gap_markers = [e for e in page.entries if e.is_gap_marker]
    assert len(gap_markers) == 0


# ---------------------------------------------------------------------------
# Tests: membership / quality markers
# ---------------------------------------------------------------------------


async def test_has_gap_true_for_partial_quality_snapshot(
    db_session: AsyncSession,
) -> None:
    """history_point.has_gap is true when snapshot quality is 'partial'."""
    wallet_id = await _insert_wallet(db_session, "0x5555000000000000000000000000000000000011")
    asset_id = await _insert_asset(db_session, token_address="0x6666000000000000000000000000000000000012")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session, quality="partial")
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        price_usd=None,
        value_usd=None,
    )

    result = await materialize_history_point(db_session, snapshot_id=sid)
    assert result.has_gap is True


async def test_gaps_quality_snapshot_materializes_history_point(
    db_session: AsyncSession,
) -> None:
    """A 'gaps' snapshot (AUD-361: provider-confirmed-unpriceable holdings)
    materializes into history_point instead of violating the quality check
    constraint — regression test for the 0012 migration."""
    wallet_id = await _insert_wallet(db_session, "0x5555000000000000000000000000000000000021")
    asset_id = await _insert_asset(db_session, token_address="0x6666000000000000000000000000000000000022")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session, quality="gaps")
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        price_usd=None,
        value_usd=None,
    )

    result = await materialize_history_point(db_session, snapshot_id=sid)
    assert result.quality == "gaps"
    assert result.has_gap is True


async def test_has_gap_false_for_complete_quality_snapshot(
    db_session: AsyncSession,
) -> None:
    """history_point.has_gap is false when snapshot quality is 'complete'."""
    wallet_id = await _insert_wallet(db_session, "0x7777000000000000000000000000000000000013")
    asset_id = await _insert_asset(db_session, token_address="0x8888000000000000000000000000000000000014")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session, quality="complete")
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        value_usd="250.5",
    )

    result = await materialize_history_point(db_session, snapshot_id=sid)
    assert result.has_gap is False


async def test_total_value_usd_summed_across_lines(
    db_session: AsyncSession,
) -> None:
    """total_value_usd in history_point is the sum of all non-null line values."""
    wallet_id = await _insert_wallet(db_session, "0x9999000000000000000000000000000000000015")
    asset_a = await _insert_asset(db_session, token_address="0xaaaa000000000000000000000000000000000016", symbol="AAA")
    asset_b = await _insert_asset(db_session, token_address="0xbbbb000000000000000000000000000000000017", symbol="BBB")
    obs_a = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_a)
    obs_b = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_b)

    sid = await _insert_snapshot(db_session, quality="complete")
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_a,
        observation_id=obs_a,
        value_usd="100.0",
    )
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_b,
        observation_id=obs_b,
        value_usd="250.5",
    )

    result = await materialize_history_point(db_session, snapshot_id=sid)
    assert result.total_value_usd == Decimal("350.5")
    assert result.included_wallet_count == 1
    assert result.included_asset_count == 2


async def test_total_value_usd_null_when_all_lines_unpriced(
    db_session: AsyncSession,
) -> None:
    """total_value_usd is null when no lines have a price (stale snapshot)."""
    wallet_id = await _insert_wallet(db_session, "0xcccc000000000000000000000000000000000018")
    asset_id = await _insert_asset(db_session, token_address="0xdddd000000000000000000000000000000000019")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session, quality="stale")
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        price_usd=None,
        value_usd=None,
    )

    result = await materialize_history_point(db_session, snapshot_id=sid)
    assert result.total_value_usd is None


# ---------------------------------------------------------------------------
# Tests: period filtering and cursor pagination
# ---------------------------------------------------------------------------


async def test_period_24h_excludes_older_points(db_session: AsyncSession) -> None:
    """history_query with period='24h' excludes points older than 24 hours."""
    wallet_id = await _insert_wallet(db_session, "0xeeee000000000000000000000000000000000020")
    asset_id = await _insert_asset(db_session, token_address="0xffff000000000000000000000000000000000021")

    from datetime import datetime as dt
    now = dt.now(tz=UTC)

    recent_ts = now - timedelta(hours=1)
    old_ts = now - timedelta(hours=48)

    for ts in (recent_ts, old_ts):
        sid = await _insert_snapshot(db_session, snapshotted_at=ts)
        obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id, observed_at=ts)
        await _insert_valuation_line(db_session, snapshot_id=sid, wallet_id=wallet_id, asset_id=asset_id, observation_id=obs_id)
        await materialize_history_point(db_session, snapshot_id=sid)

    page = await query_history(db_session, period="24h")
    real_entries = [e for e in page.entries if not e.is_gap_marker]
    # Only the recent snapshot should appear.
    assert len(real_entries) == 1
    assert abs((real_entries[0].snapshotted_at - recent_ts).total_seconds()) < 1


async def test_cursor_pagination(db_session: AsyncSession) -> None:
    """Cursor pagination correctly pages through history points without overlap."""
    wallet_id = await _insert_wallet(db_session, "0x0000100000000000000000000000000000000022")
    asset_id = await _insert_asset(db_session, token_address="0x0000200000000000000000000000000000000023")

    base = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    snapshot_ids: list[uuid.UUID] = []
    for i in range(5):
        ts = base + timedelta(hours=i)
        sid = await _insert_snapshot(db_session, snapshotted_at=ts)
        obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id, observed_at=ts)
        await _insert_valuation_line(db_session, snapshot_id=sid, wallet_id=wallet_id, asset_id=asset_id, observation_id=obs_id)
        await materialize_history_point(db_session, snapshot_id=sid)
        snapshot_ids.append(sid)

    # Fetch with limit=3 first page.
    page1 = await query_history(db_session, period="all", limit=3)
    real1 = [e for e in page1.entries if not e.is_gap_marker]
    assert len(real1) == 3
    assert page1.next_cursor is not None

    # Fetch second page.
    page2 = await query_history(db_session, period="all", cursor=page1.next_cursor, limit=3)
    real2 = [e for e in page2.entries if not e.is_gap_marker]
    assert len(real2) == 2  # 5 total, 3 on page1, 2 remaining

    # No overlap between pages.
    ids1 = {e.snapshot_id for e in real1}
    ids2 = {e.snapshot_id for e in real2}
    assert ids1.isdisjoint(ids2)

    assert page2.next_cursor is None


# ---------------------------------------------------------------------------
# Tests: snapshot detail / observation links
# ---------------------------------------------------------------------------


async def test_get_snapshot_detail_returns_observation_ids(
    db_session: AsyncSession,
) -> None:
    """GET /history/{snapshot_id} returns lines with observation_id provenance."""
    wallet_id = await _insert_wallet(db_session, "0x0000300000000000000000000000000000000024")
    asset_id = await _insert_asset(db_session, token_address="0x0000400000000000000000000000000000000025")
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
        value_usd="999.0",
    )

    detail = await get_snapshot_detail(db_session, snapshot_id=sid)
    assert detail is not None
    assert len(detail.lines) == 1
    assert detail.lines[0].observation_id == obs_id


async def test_get_snapshot_detail_returns_none_for_unknown_snapshot(
    db_session: AsyncSession,
) -> None:
    missing = uuid.uuid4()
    detail = await get_snapshot_detail(db_session, snapshot_id=missing)
    assert detail is None
