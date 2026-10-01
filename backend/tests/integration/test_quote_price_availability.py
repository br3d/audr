"""Tests for asset.price_unavailable_since bookkeeping in quote_refresh (AUD-361).

A holding the price provider was asked about and doesn't know (a dust/exotic
token not in CoinMarketCap's keyless map, or absent from CoinGecko's
catalog) must be distinguishable from a holding that simply hasn't been
asked about yet — only the latter should block /portfolio's total_usd. This
module checks that handle_quote_refresh sets and clears that flag correctly.

All rows are inserted within db_session (rolled-back transaction) so the
fencing checks in handle_quote_refresh find the job_run and integration
without needing separate committed sessions — same pattern as
test_provider_purge.py's quote_refresh tests.

Marker: integration
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.quotes import handle_quote_refresh

pytestmark = pytest.mark.integration


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wallet (id, address, label, status)"
            " VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    return wallet_id


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str = "TKN",
    price_unavailable: bool = False,
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset
              (id, token_address, symbol, name, decimals, source, price_unavailable_since)
            VALUES
              (:id, :addr, :sym, :sym, 18, 'manual',
               CASE WHEN :unavailable THEN now() ELSE NULL END)
            """
        ),
        {
            "id": str(asset_id),
            "addr": token_address.lower(),
            "sym": symbol,
            "unavailable": price_unavailable,
        },
    )
    return asset_id


async def _insert_balance(
    session: AsyncSession, *, wallet_id: uuid.UUID, asset_id: uuid.UUID
) -> None:
    await session.execute(
        text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, 1000000000000000000, 100, now())"
        ),
        {"id": str(uuid.uuid4()), "wallet": str(wallet_id), "asset": str(asset_id)},
    )


async def _setup_fencing(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Fencing needs an in_progress job_run and a coingecko integration row."""
    await session.execute(
        text(
            "INSERT INTO job_run (id, kind, status, max_retries)"
            " VALUES (:id, 'quote_refresh', 'in_progress', 3)"
        ),
        {"id": str(run_id)},
    )
    await session.execute(
        text(
            "INSERT INTO integration (kind, encrypted_blob)"
            " VALUES ('coingecko', :blob) ON CONFLICT (kind) DO NOTHING"
        ),
        {"blob": b"\x00"},
    )


async def _price_unavailable_since(session: AsyncSession, asset_id: uuid.UUID) -> object:
    row = (
        await session.execute(
            text("SELECT price_unavailable_since FROM asset WHERE id = :id"),
            {"id": str(asset_id)},
        )
    ).first()
    assert row is not None
    return row[0]


@pytest.mark.integration
async def test_quote_refresh_flags_unresolved_asset(db_session: AsyncSession) -> None:
    """An address the provider doesn't return a price for gets price_unavailable_since set."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "0" * 36)
    priced_addr = "0x" + "a1" * 20
    dust_addr = "0x" + "b2" * 20
    priced_id = await _insert_asset(db_session, token_address=priced_addr, symbol="PRICED")
    dust_id = await _insert_asset(db_session, token_address=dust_addr, symbol="DUST")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=priced_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=dust_id)
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    with patch(
        "audr.jobs.quotes.get_coingecko_api_key",
        new=AsyncMock(return_value="fake-key"),
    ):
        with respx.mock(assert_all_called=False) as mock_router:
            # The provider only knows about the priced token — dust is absent
            # from the response, exactly like an unlisted CoinGecko/CMC asset.
            mock_router.get(url__regex=r"coingecko").mock(
                return_value=httpx.Response(
                    200, json={priced_addr: {"usd": "3.5"}}
                )
            )
            await handle_quote_refresh(db_session, run_id)

    assert await _price_unavailable_since(db_session, priced_id) is None
    assert await _price_unavailable_since(db_session, dust_id) is not None


@pytest.mark.integration
async def test_quote_refresh_clears_flag_once_resolved(db_session: AsyncSession) -> None:
    """An asset previously flagged unavailable is cleared once the provider prices it."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "1" * 36)
    addr = "0x" + "c3" * 20
    asset_id = await _insert_asset(
        db_session, token_address=addr, symbol="RESOLVED", price_unavailable=True
    )
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    assert await _price_unavailable_since(db_session, asset_id) is not None

    with patch(
        "audr.jobs.quotes.get_coingecko_api_key",
        new=AsyncMock(return_value="fake-key"),
    ):
        with respx.mock(assert_all_called=False) as mock_router:
            mock_router.get(url__regex=r"coingecko").mock(
                return_value=httpx.Response(200, json={addr: {"usd": "1.0"}})
            )
            await handle_quote_refresh(db_session, run_id)

    assert await _price_unavailable_since(db_session, asset_id) is None
