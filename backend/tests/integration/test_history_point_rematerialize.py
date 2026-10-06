"""history_point aggregates stay true to the lines that survive (AUD-454).

history_point.total_value_usd and the two counts are denormalised from the
snapshot's valuation_lines at publication. Deleting lines afterwards used to
leave them describing rows that were gone, which is what the owner saw on the
chart: excluding an asset moved the curve only over the stretch where the stored
total had in fact been built from the lines that were still there, and a plateau
charting a deleted wallet's value would not move at all.

Each test here fails on the pre-AUD-454 code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.history import materialize_history_point, rematerialize_history_points
from audr.portfolio.history_query import query_history
from tests.integration.test_history import (
    _insert_asset,
    _insert_snapshot,
    _insert_valuation_line,
    _insert_wallet,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _point(session: AsyncSession, snapshot_id: uuid.UUID) -> tuple[str | None, int, int]:
    row = (
        await session.execute(
            sa.text(
                "SELECT total_value_usd::text, included_wallet_count, included_asset_count"
                " FROM history_point WHERE snapshot_id = :sid"
            ),
            {"sid": str(snapshot_id)},
        )
    ).first()
    assert row is not None, "history_point was removed"
    return row[0], int(row[1]), int(row[2])


async def test_rematerialize_rederives_total_from_surviving_lines(
    db_session: AsyncSession,
) -> None:
    """Deleting a line and re-materializing drops its value from the point's total."""
    wallet_a = await _insert_wallet(db_session, "0x" + "a1" * 20)
    wallet_b = await _insert_wallet(db_session, "0x" + "b2" * 20)
    asset_id = await _insert_asset(db_session, token_address="0x" + "c3" * 20)
    sid = await _insert_snapshot(db_session)

    await _insert_valuation_line(
        db_session, snapshot_id=sid, wallet_id=wallet_a, asset_id=asset_id, value_usd="100.0"
    )
    doomed = await _insert_valuation_line(
        db_session, snapshot_id=sid, wallet_id=wallet_b, asset_id=asset_id, value_usd="900.0"
    )
    point = await materialize_history_point(db_session, snapshot_id=sid)
    assert point.total_value_usd == Decimal("1000.0")
    assert point.included_wallet_count == 2

    await db_session.execute(
        sa.text("DELETE FROM valuation_line WHERE id = :id"), {"id": str(doomed)}
    )
    counts = await rematerialize_history_points(db_session, [sid])

    assert counts == {"updated": 1, "deleted": 0}
    total, wallets, assets = await _point(db_session, sid)
    assert Decimal(total or "0") == Decimal("100.0")
    assert (wallets, assets) == (1, 1)


async def test_rematerialize_nulls_the_total_when_no_value_survives(
    db_session: AsyncSession,
) -> None:
    """A point whose every surviving line is unpriced is unknown, not zero."""
    wallet_id = await _insert_wallet(db_session, "0x" + "a4" * 20)
    priced_asset = await _insert_asset(db_session, token_address="0x" + "c5" * 20, symbol="PRICED")
    dust_asset = await _insert_asset(db_session, token_address="0x" + "c6" * 20, symbol="DUST")
    sid = await _insert_snapshot(db_session, quality="gaps")

    priced = await _insert_valuation_line(
        db_session, snapshot_id=sid, wallet_id=wallet_id, asset_id=priced_asset, value_usd="42.0"
    )
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=dust_asset,
        price_usd=None,
        value_usd=None,
    )
    await materialize_history_point(db_session, snapshot_id=sid)

    await db_session.execute(
        sa.text("DELETE FROM valuation_line WHERE id = :id"), {"id": str(priced)}
    )
    await rematerialize_history_points(db_session, [sid])

    total, _, _ = await _point(db_session, sid)
    assert total is None


async def test_rematerialize_drops_points_left_without_lines(
    db_session: AsyncSession,
) -> None:
    """A snapshot with no lines left has no total to report, so its point goes."""
    wallet_id = await _insert_wallet(db_session, "0x" + "a6" * 20)
    asset_id = await _insert_asset(db_session, token_address="0x" + "c7" * 20)
    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session, snapshot_id=sid, wallet_id=wallet_id, asset_id=asset_id, value_usd="5.0"
    )
    await materialize_history_point(db_session, snapshot_id=sid)

    await db_session.execute(
        sa.text("DELETE FROM valuation_line WHERE snapshot_id = :sid"), {"sid": str(sid)}
    )
    counts = await rematerialize_history_points(db_session, [sid])

    assert counts == {"updated": 0, "deleted": 1}
    remaining = (
        await db_session.execute(
            sa.text("SELECT count(*) FROM history_point WHERE snapshot_id = :sid"),
            {"sid": str(sid)},
        )
    ).scalar_one()
    assert remaining == 0


async def test_exclusion_recut_matches_the_point_after_lines_are_deleted(
    db_session: AsyncSession,
) -> None:
    """The AUD-454 symptom: exclusion must move the whole curve, not part of it.

    GET /history re-cuts a point by subtracting the sum of its *surviving*
    excluded lines. With a stale stored total that subtraction came off a number
    those lines were never part of, so the curve moved by the wrong amount — or,
    where the excluded asset's lines were themselves gone, not at all.

    The deletion here is what `delete_wallet` does to a snapshot it shares with
    another wallet; `test_wallets.py` covers that call end to end.
    """
    keeper = await _insert_wallet(db_session, "0x" + "ab" * 20)
    doomed = await _insert_wallet(db_session, "0x" + "bc" * 20)
    kept_asset = await _insert_asset(db_session, token_address="0x" + "cd" * 20, symbol="KEEP")
    excluded_asset = await _insert_asset(db_session, token_address="0x" + "de" * 20, symbol="DROP")
    sid = await _insert_snapshot(
        db_session, snapshotted_at=datetime.now(tz=UTC) - timedelta(hours=1)
    )

    for wallet_id, asset_id, value in (
        (keeper, kept_asset, "400.0"),
        (keeper, excluded_asset, "100.0"),
        (doomed, kept_asset, "195000.0"),
    ):
        await _insert_valuation_line(
            db_session,
            snapshot_id=sid,
            wallet_id=wallet_id,
            asset_id=asset_id,
            value_usd=value,
        )
    point = await materialize_history_point(db_session, snapshot_id=sid)
    assert point.total_value_usd == Decimal("195500.0")

    await db_session.execute(
        sa.text("DELETE FROM valuation_line WHERE wallet_id = :wid"), {"wid": str(doomed)}
    )
    await rematerialize_history_points(db_session, [sid])
    await db_session.execute(
        sa.text("UPDATE asset SET excluded = true WHERE id = :aid"),
        {"aid": str(excluded_asset)},
    )

    page = await query_history(db_session, period="24h")
    entry = next(e for e in page.entries if e.snapshot_id == sid)

    # 400 (kept) + 100 (excluded) survive the line deletion; excluding DROP leaves 400.
    assert entry.total_value_usd == Decimal("400.0")
