"""Integration test: full valuation pipeline produces non-empty history and holdings (AUD-316).

Simulates one complete worker cycle without real RPC or provider calls:
1. Insert a wallet, an asset, a balance observation, and a completed quote set.
2. Run publish_valuation_snapshot + materialize_history_point (what handle_valuation does).
3. Assert GET /api/v1/portfolio/holdings and GET /api/v1/history return non-empty data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.history import materialize_history_point
from audr.portfolio.history_query import query_history
from audr.portfolio.snapshot import get_latest_snapshot_lines, publish_valuation_snapshot

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wid = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label, status)"
            " VALUES (:id, :addr, 'test', 'active')"
        ),
        {"id": str(wid), "addr": address.lower()},
    )
    return wid


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str = "WETH",
    decimals: int = 18,
) -> uuid.UUID:
    aid = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)"
            " VALUES (:id, :addr, :sym, :name, :dec, 'manual', false)"
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


async def _insert_balance(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int = 1_500_000_000_000_000_000,
    block_number: int = 20_000_000,
) -> None:
    await session.execute(
        sa.text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, :raw, :block, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
        },
    )


async def _insert_complete_quote_set(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    price_usd: Decimal,
) -> uuid.UUID:
    qset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now(), 'complete')"
        ),
        {"id": str(qset_id)},
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_valuation_pipeline_populates_holdings_and_history(
    db_session: AsyncSession,
) -> None:
    """After one valuation cycle, holdings and history are non-empty."""
    wallet_id = await _insert_wallet(db_session, "0x" + "a1" * 20)
    asset_id = await _insert_asset(
        db_session,
        token_address="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        symbol="WETH",
        decimals=18,
    )
    # 1.5 ETH
    await _insert_balance(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        raw_amount=1_500_000_000_000_000_000,
    )
    await _insert_complete_quote_set(
        db_session, asset_id=asset_id, price_usd=Decimal("3000")
    )
    await db_session.flush()

    # Run the valuation pipeline (mirrors handle_valuation).
    snap = await publish_valuation_snapshot(db_session)
    hp = await materialize_history_point(db_session, snapshot_id=snap.snapshot_id)
    await db_session.flush()

    # holdings API backend returns non-empty rows
    lines = await get_latest_snapshot_lines(db_session)
    assert len(lines) >= 1, "portfolio/holdings should return at least one row"
    assert lines[0]["symbol"] == "WETH"
    assert Decimal(lines[0]["value_usd"]) == Decimal("4500")  # 1.5 * 3000

    # history API backend returns non-empty entries
    page = await query_history(db_session, period="all")
    assert len(page.entries) >= 1, "history should return at least one entry"
    entry = page.entries[0]
    assert not entry.is_gap_marker
    assert entry.total_value_usd == Decimal("4500")
    assert entry.quality == "complete"
    assert entry.included_asset_count == 1
    assert entry.included_wallet_count == 1


@pytest.mark.integration
async def test_valuation_pipeline_quality_stale_no_prices(
    db_session: AsyncSession,
) -> None:
    """Snapshot quality is 'stale' when balances exist but no prices are available."""
    wallet_id = await _insert_wallet(db_session, "0x" + "b2" * 20)
    asset_id = await _insert_asset(
        db_session,
        token_address="0x" + "cc" * 20,
        symbol="UNKN",
        decimals=18,
    )
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    # No quote set inserted.
    await db_session.flush()

    snap = await publish_valuation_snapshot(db_session)
    hp = await materialize_history_point(db_session, snapshot_id=snap.snapshot_id)
    await db_session.flush()

    assert snap.quality == "stale"
    assert hp.has_gap is True
    assert hp.total_value_usd is None

    # Holdings line exists but value_usd is null.
    lines = await get_latest_snapshot_lines(db_session)
    assert len(lines) >= 1
    assert lines[0]["value_usd"] is None

    # History still surfaces the entry (with gap marker).
    page = await query_history(db_session, period="all")
    real_entries = [e for e in page.entries if not e.is_gap_marker]
    assert len(real_entries) >= 1
    assert real_entries[0].total_value_usd is None


@pytest.mark.integration
async def test_handle_valuation_is_idempotent_on_same_snapshot(
    db_session: AsyncSession,
) -> None:
    """materialize_history_point called twice for the same snapshot returns the same row."""
    wallet_id = await _insert_wallet(db_session, "0x" + "c3" * 20)
    asset_id = await _insert_asset(
        db_session,
        token_address="0x" + "dd" * 20,
        symbol="TKN",
        decimals=18,
    )
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_complete_quote_set(
        db_session, asset_id=asset_id, price_usd=Decimal("1")
    )
    await db_session.flush()

    snap = await publish_valuation_snapshot(db_session)
    hp1 = await materialize_history_point(db_session, snapshot_id=snap.snapshot_id)
    hp2 = await materialize_history_point(db_session, snapshot_id=snap.snapshot_id)

    assert hp1.history_point_id == hp2.history_point_id
    assert hp2.created is False
