"""AUD-446: a stopped wallet must not keep contributing current holdings.

handle_balance_scan only ever scans ``active`` wallets, so a stopped wallet's
newest balance_observation is frozen at whatever block it was stopped on. While
_get_current_holdings still picked those rows up, every snapshot carried them
forward at that old block_number — and api/portfolio.py flags any line below the
snapshot's max block_number as "stale". The result was a permanent Stale badge
on the stopped wallet's assets that "Refresh balances" could never clear,
because the one job that could advance those blocks skips the wallet by design.

Tests use the rolled-back db_session fixture — no permanent state.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.snapshot import _get_current_holdings, publish_valuation_snapshot
from tests.helpers import wallet_address_columns


async def _insert_wallet(session: AsyncSession, address: str, *, status: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    cols = await wallet_address_columns(session, wallet_id, address)
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address_ciphertext, address_bidx, label_ciphertext, status)"
            " VALUES (:id, :addr_ct, :addr_bidx, '', :status)"
        ),
        {
            "id": str(wallet_id),
            "addr_ct": cols["address_ciphertext"],
            "addr_bidx": cols["address_bidx"],
            "status": status,
        },
    )
    return wallet_id


async def _insert_asset(session: AsyncSession, *, token_address: str, symbol: str) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source)
            VALUES (:id, :addr, :sym, :sym, 18, 'manual')
            """
        ),
        {"id": str(asset_id), "addr": token_address.lower(), "sym": symbol},
    )
    return asset_id


async def _insert_balance(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int,
    block_number: int,
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
    session: AsyncSession, *, asset_ids: list[uuid.UUID], price_usd: Decimal
) -> None:
    qset_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status)"
            " VALUES (:id, 'coingecko', now(), 'complete')"
        ),
        {"id": str(qset_id)},
    )
    for asset_id in asset_ids:
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


@pytest.mark.integration
async def test_stopped_wallet_excluded_from_current_holdings(db_session: AsyncSession) -> None:
    """Only active wallets contribute rows to _get_current_holdings."""
    active_id = await _insert_wallet(db_session, "0x" + "1" * 40, status="active")
    stopped_id = await _insert_wallet(db_session, "0x" + "2" * 40, status="stopped")
    asset_id = await _insert_asset(db_session, token_address="0x" + "3" * 40, symbol="TKN")

    await _insert_balance(
        db_session, wallet_id=active_id, asset_id=asset_id, raw_amount=10**18, block_number=200
    )
    await _insert_balance(
        db_session, wallet_id=stopped_id, asset_id=asset_id, raw_amount=5 * 10**18, block_number=100
    )
    await db_session.flush()

    holdings = await _get_current_holdings(db_session)

    assert [h.wallet_id for h in holdings] == [active_id]


@pytest.mark.integration
async def test_stopped_wallet_does_not_pin_snapshot_to_an_old_block(
    db_session: AsyncSession,
) -> None:
    """No snapshot line may sit below the max block — that is what renders as Stale.

    The stopped wallet's observation is deliberately several blocks behind the
    active wallet's, which is exactly the shape that produced the permanent
    "Stale" badges in the field report.
    """
    active_id = await _insert_wallet(db_session, "0x" + "4" * 40, status="active")
    stopped_id = await _insert_wallet(db_session, "0x" + "5" * 40, status="stopped")
    asset_a = await _insert_asset(db_session, token_address="0x" + "6" * 40, symbol="AAA")
    asset_b = await _insert_asset(db_session, token_address="0x" + "7" * 40, symbol="BBB")

    await _insert_balance(
        db_session,
        wallet_id=active_id,
        asset_id=asset_a,
        raw_amount=10**18,
        block_number=26_132_475,
    )
    await _insert_balance(
        db_session,
        wallet_id=stopped_id,
        asset_id=asset_b,
        raw_amount=7 * 10**18,
        block_number=26_125_855,
    )
    await _insert_quote_set(db_session, asset_ids=[asset_a, asset_b], price_usd=Decimal("10"))
    await db_session.flush()

    result = await publish_valuation_snapshot(db_session)

    blocks = (
        (
            await db_session.execute(
                sa.text(
                    "SELECT DISTINCT block_number FROM valuation_line WHERE snapshot_id = :snap"
                ),
                {"snap": str(result.snapshot_id)},
            )
        )
        .scalars()
        .all()
    )

    assert blocks == [26_132_475]
    assert result.line_count == 1
