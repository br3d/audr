"""Integration tests for quote jobs and valuation snapshots (T049 / AUD-62).

Tests use the rolled-back db_session fixture — no permanent state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.money import format_decimal, quantity_to_usd, raw_to_quantity
from audr.portfolio.snapshot import _compute_quality, publish_valuation_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str = "TKN",
    decimals: int = 18,
    excluded: bool = False,
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
            VALUES (:id, :addr, :sym, :name, :dec, 'manual', :excluded)
            """
        ),
        {
            "id": str(asset_id),
            "addr": token_address.lower(),
            "sym": symbol,
            "name": symbol,
            "dec": decimals,
            "excluded": excluded,
        },
    )
    return asset_id


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label, status) VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    return wallet_id


async def _insert_balance(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int,
    block_number: int = 12345678,
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, observed_at)
            VALUES (:id, :wallet, :asset, :raw, :block, now())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
        },
    )


async def _insert_quote_set(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    price_usd: Decimal,
    status: str = "complete",
) -> uuid.UUID:
    qset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status) VALUES (:id, 'coingecko', now(), :status)"
        ),
        {"id": str(qset_id), "status": status},
    )
    await session.execute(
        sa.text(
            "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd) VALUES (:id, :qset, :asset, :price)"
        ),
        {
            "id": str(uuid.uuid4()),
            "qset": str(qset_id),
            "asset": str(asset_id),
            "price": str(price_usd),
        },
    )
    return qset_id


# ---------------------------------------------------------------------------
# money.py round-trip tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestMoneyRoundTrip:
    def test_eth_round_trip(self) -> None:
        raw = 1_500_000_000_000_000_000  # 1.5 ETH
        qty = raw_to_quantity(raw, 18)
        assert qty == Decimal("1.5")
        usd = quantity_to_usd(qty, Decimal("2000"))
        assert usd == Decimal("3000")
        assert format_decimal(usd, places=2) == "3000.00"

    def test_usdc_round_trip(self) -> None:
        raw = 100_000_000  # 100 USDC (6 decimals)
        qty = raw_to_quantity(raw, 6)
        assert qty == Decimal("100")
        usd = quantity_to_usd(qty, Decimal("1"))
        assert usd == Decimal("100")


# ---------------------------------------------------------------------------
# Quality computation (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestComputeQuality:
    def test_empty_holdings_unknown(self) -> None:
        quality, priced = _compute_quality([], {})
        assert quality == "unknown"
        assert priced == 0

    def test_all_priced_complete(self) -> None:
        from audr.portfolio.snapshot import HoldingRow

        aid = uuid.uuid4()
        holding = HoldingRow(
            wallet_id=uuid.uuid4(),
            asset_id=aid,
            token_address="0x" + "a" * 40,
            raw_amount=1,
            block_number=1,
            decimals=18,
        )
        quality, priced = _compute_quality([holding], {aid: Decimal("1")})
        assert quality == "complete"
        assert priced == 1

    def test_some_unpriced_partial(self) -> None:
        from audr.portfolio.snapshot import HoldingRow

        aid1 = uuid.uuid4()
        aid2 = uuid.uuid4()
        h1 = HoldingRow(uuid.uuid4(), aid1, "0x" + "a" * 40, 1, 1, 18)
        h2 = HoldingRow(uuid.uuid4(), aid2, "0x" + "b" * 40, 1, 1, 18)
        quality, priced = _compute_quality([h1, h2], {aid1: Decimal("1")})
        assert quality == "partial"
        assert priced == 1

    def test_no_prices_stale(self) -> None:
        from audr.portfolio.snapshot import HoldingRow

        aid = uuid.uuid4()
        holding = HoldingRow(uuid.uuid4(), aid, "0x" + "a" * 40, 1, 1, 18)
        quality, priced = _compute_quality([holding], {})
        assert quality == "stale"
        assert priced == 0


# ---------------------------------------------------------------------------
# Snapshot publishing DB integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_publish_snapshot_creates_rows(db_session: AsyncSession) -> None:
    """publish_valuation_snapshot creates snapshot and line rows."""
    wallet_id = await _insert_wallet(db_session, "0x" + "1" * 40)
    asset_id = await _insert_asset(db_session, token_address="0x" + "2" * 40, decimals=18)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18)
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("2000"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    assert result.quality == "complete"
    assert result.line_count == 1
    assert result.priced_count == 1

    snap_row = await db_session.execute(
        sa.text("SELECT quality, published_at FROM valuation_snapshot WHERE id = :id"),
        {"id": str(result.snapshot_id)},
    )
    row = snap_row.first()
    assert row is not None
    assert row[0] == "complete"
    assert row[1] is not None  # published_at is set


@pytest.mark.integration
async def test_publish_snapshot_value_usd_exact(db_session: AsyncSession) -> None:
    """value_usd = raw / 10^decimals * price_usd, stored as exact Decimal."""
    wallet_id = await _insert_wallet(db_session, "0x" + "3" * 40)
    asset_id = await _insert_asset(db_session, token_address="0x" + "4" * 40, decimals=6)
    # 500 USDC (6 decimals)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=500_000_000)
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("1"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    line_row = await db_session.execute(
        sa.text("SELECT value_usd::text FROM valuation_line WHERE snapshot_id = :snap"),
        {"snap": str(result.snapshot_id)},
    )
    row = line_row.first()
    assert row is not None
    stored_value = Decimal(row[0])
    assert stored_value == Decimal("500")


@pytest.mark.integration
async def test_publish_snapshot_unknown_price_is_null(db_session: AsyncSession) -> None:
    """Holding without a price gets NULL value_usd — unknown is never zero."""
    wallet_id = await _insert_wallet(db_session, "0x" + "5" * 40)
    # asset with no corresponding quote
    asset_id = await _insert_asset(db_session, token_address="0x" + "6" * 40, decimals=18)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18)
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    assert result.quality == "stale"
    assert result.priced_count == 0

    line_row = await db_session.execute(
        sa.text("SELECT price_usd, value_usd FROM valuation_line WHERE snapshot_id = :snap"),
        {"snap": str(result.snapshot_id)},
    )
    row = line_row.first()
    assert row is not None
    assert row[0] is None  # price_usd is NULL
    assert row[1] is None  # value_usd is NULL


@pytest.mark.integration
async def test_publish_snapshot_excluded_asset_omitted(db_session: AsyncSession) -> None:
    """Excluded assets are not included in the snapshot."""
    wallet_id = await _insert_wallet(db_session, "0x" + "7" * 40)
    included_id = await _insert_asset(
        db_session, token_address="0x" + "8" * 40, excluded=False
    )
    excluded_id = await _insert_asset(
        db_session, token_address="0x" + "9" * 40, excluded=True
    )
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=included_id, raw_amount=10**18)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=excluded_id, raw_amount=10**18)
    await _insert_quote_set(db_session, asset_id=included_id, price_usd=Decimal("100"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    # Only the non-excluded asset appears
    assert result.line_count == 1
    line_check = await db_session.execute(
        sa.text("SELECT asset_id FROM valuation_line WHERE snapshot_id = :snap"),
        {"snap": str(result.snapshot_id)},
    )
    asset_ids = [str(row[0]) for row in line_check]
    assert str(included_id) in asset_ids
    assert str(excluded_id) not in asset_ids


@pytest.mark.integration
async def test_publish_snapshot_no_holdings_raises(db_session: AsyncSession) -> None:
    """publish_valuation_snapshot raises ValueError when there are no holdings."""
    with pytest.raises(ValueError, match="no holdings"):
        await publish_valuation_snapshot(db_session)


@pytest.mark.integration
async def test_stale_balance_uses_latest_observation(db_session: AsyncSession) -> None:
    """The latest observation per wallet/asset is used, not the first."""
    wallet_id = await _insert_wallet(db_session, "0x" + "a" * 40)
    asset_id = await _insert_asset(db_session, token_address="0x" + "b" * 40, decimals=18)

    # Insert two observations; second should win.
    await session_execute_with_delay(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        raw_amount=10**18,
        block_number=100,
    )
    await session_execute_with_delay(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        raw_amount=2 * 10**18,
        block_number=200,
    )
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("1"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    line_row = await db_session.execute(
        sa.text("SELECT raw_amount::text, block_number FROM valuation_line WHERE snapshot_id = :snap"),
        {"snap": str(result.snapshot_id)},
    )
    row = line_row.first()
    assert row is not None
    assert int(row[0]) == 2 * 10**18  # latest balance
    assert row[1] == 200  # latest block


async def session_execute_with_delay(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int,
    block_number: int,
) -> None:
    """Insert balance observation with a clock offset so observed_at ordering works."""
    await session.execute(
        sa.text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, observed_at)
            VALUES (:id, :wallet, :asset, :raw, :block, now() + (:block * interval '1 millisecond'))
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
        },
    )
