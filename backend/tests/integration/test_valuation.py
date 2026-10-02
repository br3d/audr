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

from audr.portfolio.history import materialize_history_point
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
    price_unavailable: bool = False,
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO asset
              (id, token_address, symbol, name, decimals, source, excluded, price_unavailable_since)
            VALUES
              (:id, :addr, :sym, :name, :dec, 'manual', :excluded,
               CASE WHEN :unavailable THEN now() ELSE NULL END)
            """
        ),
        {
            "id": str(asset_id),
            "addr": token_address.lower(),
            "sym": symbol,
            "name": symbol,
            "dec": decimals,
            "excluded": excluded,
            "unavailable": price_unavailable,
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
    block_time: datetime | None = None,
) -> None:
    await session.execute(
        sa.text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, block_time, observed_at)
            VALUES (:id, :wallet, :asset, :raw, :block, :block_time, now())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
            "block_time": block_time,
        },
    )


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
            observation_id=uuid.uuid4(),
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
        h1 = HoldingRow(uuid.uuid4(), aid1, uuid.uuid4(), "0x" + "a" * 40, 1, 1, 18)
        h2 = HoldingRow(uuid.uuid4(), aid2, uuid.uuid4(), "0x" + "b" * 40, 1, 1, 18)
        quality, priced = _compute_quality([h1, h2], {aid1: Decimal("1")})
        assert quality == "partial"
        assert priced == 1

    def test_no_prices_stale(self) -> None:
        from audr.portfolio.snapshot import HoldingRow

        aid = uuid.uuid4()
        holding = HoldingRow(uuid.uuid4(), aid, uuid.uuid4(), "0x" + "a" * 40, 1, 1, 18)
        quality, priced = _compute_quality([holding], {})
        assert quality == "stale"
        assert priced == 0

    def test_unpriced_but_provider_confirmed_unavailable_is_gaps(self) -> None:
        """A holding the provider confirmed it doesn't know is 'gaps', not 'partial' (AUD-361).

        'gaps' means total_usd is still reachable from what *is* priced — the
        dust/exotic-token scenario from AUD-361 where 4 of 88 holdings will
        never resolve against the keyless CoinMarketCap map.
        """
        from audr.portfolio.snapshot import HoldingRow

        aid1 = uuid.uuid4()
        aid2 = uuid.uuid4()
        priced_holding = HoldingRow(uuid.uuid4(), aid1, uuid.uuid4(), "0x" + "a" * 40, 1, 1, 18)
        unavailable_holding = HoldingRow(
            uuid.uuid4(), aid2, uuid.uuid4(), "0x" + "b" * 40, 1, 1, 18, price_unavailable=True
        )
        quality, priced = _compute_quality(
            [priced_holding, unavailable_holding], {aid1: Decimal("1")}
        )
        assert quality == "gaps"
        assert priced == 1

    def test_unavailable_and_never_asked_mix_is_partial(self) -> None:
        """A genuinely-never-asked holding keeps the snapshot 'partial', even
        alongside a confirmed-unavailable one — it could still resolve on the
        next refresh, so the total must stay blocked.
        """
        from audr.portfolio.snapshot import HoldingRow

        aid1 = uuid.uuid4()
        aid2 = uuid.uuid4()
        aid3 = uuid.uuid4()
        priced_holding = HoldingRow(uuid.uuid4(), aid1, uuid.uuid4(), "0x" + "a" * 40, 1, 1, 18)
        unavailable_holding = HoldingRow(
            uuid.uuid4(), aid2, uuid.uuid4(), "0x" + "b" * 40, 1, 1, 18, price_unavailable=True
        )
        never_asked_holding = HoldingRow(uuid.uuid4(), aid3, uuid.uuid4(), "0x" + "c" * 40, 1, 1, 18)
        quality, priced = _compute_quality(
            [priced_holding, unavailable_holding, never_asked_holding], {aid1: Decimal("1")}
        )
        assert quality == "partial"
        assert priced == 1


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
async def test_publish_snapshot_copies_block_time_from_observation(
    db_session: AsyncSession,
) -> None:
    """valuation_line.block_time is denormalized from the source balance_observation (AUD-72).

    It must survive on the immutable snapshot row even if the observation it
    came from is later superseded by a newer scan.
    """
    wallet_id = await _insert_wallet(db_session, "0x" + "7" * 40)
    asset_id = await _insert_asset(db_session, token_address="0x" + "8" * 40, decimals=18)
    chain_block_time = datetime(2026, 1, 1, tzinfo=UTC)
    await _insert_balance(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        raw_amount=10**18,
        block_time=chain_block_time,
    )
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("2000"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    line_row = await db_session.execute(
        sa.text("SELECT block_time FROM valuation_line WHERE snapshot_id = :snap"),
        {"snap": str(result.snapshot_id)},
    )
    row = line_row.first()
    assert row is not None
    assert row[0] == chain_block_time


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
async def test_publish_snapshot_provider_confirmed_gap_is_quality_gaps(
    db_session: AsyncSession,
) -> None:
    """A holding whose asset is flagged price_unavailable_since yields 'gaps' (AUD-361).

    This is the exact AUD-361 scenario: a dust/exotic holding the keyless
    CoinMarketCap map will never resolve must not keep the whole portfolio
    total unreachable — its line still gets a NULL price (unknown ≠ zero),
    but the snapshot quality distinguishes it from a holding that was simply
    never asked about.
    """
    wallet_id = await _insert_wallet(db_session, "0x" + "f" * 40)
    priced_id = await _insert_asset(db_session, token_address="0x" + "1a" * 20, decimals=18)
    dust_id = await _insert_asset(
        db_session,
        token_address="0x" + "1b" * 20,
        decimals=18,
        price_unavailable=True,
    )
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=priced_id, raw_amount=10**18)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=dust_id, raw_amount=10**18)
    await _insert_quote_set(db_session, asset_id=priced_id, price_usd=Decimal("100"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    assert result.quality == "gaps"
    assert result.priced_count == 1

    dust_line = await db_session.execute(
        sa.text(
            "SELECT price_usd, value_usd FROM valuation_line"
            " WHERE snapshot_id = :snap AND asset_id = :asset"
        ),
        {"snap": str(result.snapshot_id), "asset": str(dust_id)},
    )
    row = dust_line.first()
    assert row is not None
    assert row[0] is None  # unknown ≠ zero even when quality is 'gaps'
    assert row[1] is None


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


@pytest.mark.integration
async def test_empty_complete_set_does_not_shadow_prior_prices(
    db_session: AsyncSession,
) -> None:
    """A newer complete quote_set with zero observations must not shadow older valid prices.

    Regression (AUD-273): _get_latest_prices selects MAX(fetched_at) from complete
    sets. If the newest complete set has no observations, it silently returns {},
    turning a priced portfolio stale even though valid prices exist in an earlier set.
    """
    wallet_id = await _insert_wallet(db_session, "0x" + "c" * 40)
    asset_id = await _insert_asset(db_session, token_address="0x" + "d" * 40, decimals=18)
    await _insert_balance(
        db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18, block_number=1
    )
    await db_session.flush()

    # Older complete set with a real price.
    good_id = uuid.uuid4()
    await db_session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now() - interval '10 minutes', 'complete')"
        ),
        {"id": str(good_id)},
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)"
            " VALUES (:id, :qset, :asset, :price)"
        ),
        {"id": str(uuid.uuid4()), "qset": str(good_id), "asset": str(asset_id), "price": "1500"},
    )

    # Newer complete set with zero observations — the bug scenario.
    empty_id = uuid.uuid4()
    await db_session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now(), 'complete')"
        ),
        {"id": str(empty_id)},
    )
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    assert result.quality != "stale", (
        "empty complete set must not shadow valid prices from an earlier set"
    )
    assert result.priced_count == 1


# ---------------------------------------------------------------------------
# Input-key deduplication (AUD-70)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_publish_snapshot_retry_same_inputs_does_not_add_history(
    db_session: AsyncSession,
) -> None:
    """Retrying publish with the exact same observation/quote inputs must not add history."""
    wallet_id = await _insert_wallet(db_session, "0x" + "e1" * 20)
    asset_id = await _insert_asset(db_session, token_address="0x" + "e2" * 20, decimals=18)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18)
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("2000"))
    await db_session.flush()

    first = await publish_valuation_snapshot(db_session)
    assert first.created is True
    await materialize_history_point(db_session, snapshot_id=first.snapshot_id)

    # Retry with no new observations or quotes at all.
    second = await publish_valuation_snapshot(db_session)
    assert second.created is False
    assert second.snapshot_id == first.snapshot_id
    await materialize_history_point(db_session, snapshot_id=second.snapshot_id)

    snapshot_count = await db_session.execute(
        sa.text("SELECT COUNT(*) FROM valuation_snapshot")
    )
    assert snapshot_count.scalar_one() == 1

    history_count = await db_session.execute(
        sa.text("SELECT COUNT(*) FROM history_point")
    )
    assert history_count.scalar_one() == 1


@pytest.mark.integration
async def test_publish_snapshot_later_verified_block_adds_history(
    db_session: AsyncSession,
) -> None:
    """A later verified block with unchanged quantities still has a new observation id,
    so it adds a new snapshot and history point (the input key differs by block)."""
    wallet_id = await _insert_wallet(db_session, "0x" + "e3" * 20)
    asset_id = await _insert_asset(db_session, token_address="0x" + "e4" * 20, decimals=18)
    await session_execute_with_delay(
        db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18, block_number=100
    )
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("2000"))
    await db_session.flush()

    first = await publish_valuation_snapshot(db_session)
    assert first.created is True

    # Same quantity, later verified block — a new observation row, same raw_amount.
    await session_execute_with_delay(
        db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18, block_number=200
    )
    await db_session.flush()

    second = await publish_valuation_snapshot(db_session)
    assert second.created is True
    assert second.snapshot_id != first.snapshot_id


@pytest.mark.integration
async def test_publish_snapshot_quote_set_change_adds_history(
    db_session: AsyncSession,
) -> None:
    """Changing only the quote set (holdings unchanged) still adds a new snapshot."""
    wallet_id = await _insert_wallet(db_session, "0x" + "e5" * 20)
    asset_id = await _insert_asset(db_session, token_address="0x" + "e6" * 20, decimals=18)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id, raw_amount=10**18)
    await _insert_quote_set(db_session, asset_id=asset_id, price_usd=Decimal("2000"))
    await db_session.flush()

    first = await publish_valuation_snapshot(db_session)
    assert first.created is True

    # A fresh quote_set, strictly newer, same price — holdings are unchanged but
    # the quote data's provenance (quote_set id) is new.
    await _insert_quote_set(
        db_session, asset_id=asset_id, price_usd=Decimal("2100"), offset_minutes=10
    )
    await db_session.flush()

    second = await publish_valuation_snapshot(db_session)
    assert second.created is True
    assert second.snapshot_id != first.snapshot_id
