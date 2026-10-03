"""Regression tests for uint256-sized balances (AUD P0 ``raw_amount::bigint``).

The original defect: ``balance_observation.raw_amount`` is ``Numeric(78,0)``
(wei / uint256) but the holdings and snapshot queries cast it to ``::bigint``
(max 9.22e18), so any wallet holding >= ~9.23 units of an 18-decimal token made
Postgres raise ``bigint out of range`` — the holdings endpoint 500'd and
``publish_valuation_snapshot`` failed. Both queries now cast to ``::numeric``;
these tests pin that with balances above 2**63.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.balances import get_holdings, record_balance
from audr.portfolio.snapshot import publish_valuation_snapshot

pytestmark = pytest.mark.integration

# 10,000 tokens with 18 decimals — ~1084x past the bigint ceiling.
HUGE_RAW = 10_000 * 10**18
assert HUGE_RAW > 2**63


async def test_get_holdings_handles_balance_above_bigint_max(
    db_session: AsyncSession,
) -> None:
    """A uint256-sized balance round-trips through get_holdings exactly."""
    wallet = "0x" + "7" * 40
    token = "0x" + "8" * 40

    await record_balance(
        db_session,
        wallet_address=wallet,
        token_address=token,
        raw_amount=HUGE_RAW,
        block_number=19_000_000,
    )

    holdings = await get_holdings(db_session, wallet_address=wallet)
    match = next((h for h in holdings if h.token_address.lower() == token.lower()), None)
    assert match is not None
    assert match.raw_amount == HUGE_RAW


async def test_publish_snapshot_handles_balance_above_bigint_max(
    db_session: AsyncSession,
) -> None:
    """publish_valuation_snapshot values a uint256-sized holding without overflow."""
    wallet_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    qset_id = uuid.uuid4()

    await db_session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label, status) VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": "0x" + "9" * 40},
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)"
            " VALUES (:id, :addr, 'BIG', 'Big Token', 18, 'manual', false)"
        ),
        {"id": str(asset_id), "addr": "0x" + "a" * 39 + "1"},
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, :raw, 19000000, now())"
        ),
        {
            "id": str(uuid.uuid4()),
            "wallet": str(wallet_id),
            "asset": str(asset_id),
            "raw": HUGE_RAW,
        },
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now(), 'complete')"
        ),
        {"id": str(qset_id)},
    )
    await db_session.execute(
        sa.text(
            "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)"
            " VALUES (:id, :qset, :asset, '3')"
        ),
        {"id": str(uuid.uuid4()), "qset": str(qset_id), "asset": str(asset_id)},
    )
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    assert result.line_count >= 1
    assert result.priced_count >= 1

    line = await db_session.execute(
        sa.text(
            "SELECT value_usd::text FROM valuation_line"
            " WHERE snapshot_id = :snap AND asset_id = :asset"
        ),
        {"snap": str(result.snapshot_id), "asset": str(asset_id)},
    )
    row = line.first()
    assert row is not None
    # 10,000 tokens * $3 = $30,000 exactly.
    assert Decimal(row[0]) == Decimal("30000")
