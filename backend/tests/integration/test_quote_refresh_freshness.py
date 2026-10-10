"""Per-asset freshness caching in quote_refresh (AUD-370).

A held asset whose most recent price is still younger than freshness_s must
be skipped on the provider call and have its last known price carried
forward into the new quote_set — only genuinely stale assets should ever
reach CoinMarketCap. This is what keeps a run triggered sooner than a full
schedule interval from re-asking the provider about assets nothing has
changed for, which is exactly the kind of extra request volume that trips
CoinMarketCap's anonymous rate limit.

Marker: integration
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.quotes import handle_quote_refresh
from tests.helpers import wallet_address_columns

pytestmark = pytest.mark.integration

_CMC_BASE = "https://pro-api.coinmarketcap.com"
_FRESH_CMC_ID = 1001
_STALE_CMC_ID = 1002


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    cols = await wallet_address_columns(session, wallet_id, address)
    await session.execute(
        text(
            "INSERT INTO wallet (id, address_ciphertext, address_bidx, label_ciphertext, status)"
            " VALUES (:id, :addr_ct, :addr_bidx, '', 'active')"
        ),
        {
            "id": str(wallet_id),
            "addr_ct": cols["address_ciphertext"],
            "addr_bidx": cols["address_bidx"],
        },
    )
    return wallet_id


async def _insert_asset(session: AsyncSession, *, token_address: str, symbol: str) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source)
            VALUES (:id, :addr, :sym, :sym, 18, 'manual')
            """
        ),
        {"id": str(asset_id), "addr": token_address.lower(), "sym": symbol},
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


async def _insert_complete_quote_set(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    price_usd: Decimal,
    fetched_at_sql: str,
) -> None:
    """Insert a complete quote_set/observation pair for *asset_id*.

    *fetched_at_sql* is a raw SQL expression (e.g. "now()" or
    "now() - interval '2 hours'") interpolated directly — test-only, never
    user input.
    """
    quote_set_id = uuid.uuid4()
    await session.execute(
        text(
            f"""
            INSERT INTO quote_set (id, provider, fetched_at, status, created_at)
            VALUES (:id, 'coinmarketcap', {fetched_at_sql}, 'complete', now())
            """  # noqa: S608 — fetched_at_sql is a module-local constant, not user input
        ),
        {"id": str(quote_set_id)},
    )
    await session.execute(
        text(
            "INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)"
            " VALUES (:id, :qset, :asset, :price)"
        ),
        {
            "id": str(uuid.uuid4()),
            "qset": str(quote_set_id),
            "asset": str(asset_id),
            "price": str(price_usd),
        },
    )


async def _setup_fencing(session: AsyncSession, run_id: uuid.UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO job_run (id, kind, status, max_retries)"
            " VALUES (:id, 'quote_refresh', 'in_progress', 3)"
        ),
        {"id": str(run_id)},
    )


async def _latest_quote_set_prices(session: AsyncSession) -> dict[str, Decimal]:
    rows = await session.execute(
        text(
            """
            SELECT a.token_address, qo.price_usd
            FROM quote_observation qo
            JOIN asset a ON a.id = qo.asset_id
            WHERE qo.quote_set_id = (
                SELECT id FROM quote_set ORDER BY created_at DESC LIMIT 1
            )
            """
        )
    )
    return {row[0]: Decimal(row[1]) for row in rows}


@pytest.mark.integration
async def test_fresh_price_is_carried_forward_without_a_provider_call(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """An asset priced moments ago is not re-requested; its price is reused."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "2" * 36)
    fresh_addr = "0x" + "d4" * 20
    stale_addr = "0x" + "e5" * 20
    fresh_id = await _insert_asset(db_session, token_address=fresh_addr, symbol="FRESH")
    stale_id = await _insert_asset(db_session, token_address=stale_addr, symbol="STALE")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=fresh_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=stale_id)
    await _insert_complete_quote_set(
        db_session, asset_id=fresh_id, price_usd=Decimal("5.00"), fetched_at_sql="now()"
    )
    # stale_id has no quote history at all — "never priced" is always stale.
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    resolver_calls: list[list[str]] = []

    async def _fake_resolver(_session: AsyncSession, addresses: list[str]) -> dict[str, int]:
        resolver_calls.append(list(addresses))
        return {stale_addr: _STALE_CMC_ID}

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
        with patch("audr.jobs.quotes.resolve_cmc_ids", new=_fake_resolver):
            with respx.mock(assert_all_called=True) as mock_router:
                mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "data": [{"id": _STALE_CMC_ID, "price": 42.0}],
                            "status": {"error_code": "0"},
                        },
                    )
                )
                await handle_quote_refresh(db_session, run_id)

    # Only the stale address was ever sent to the resolver / provider.
    assert resolver_calls == [[stale_addr]]

    prices = await _latest_quote_set_prices(db_session)
    assert prices[fresh_addr] == Decimal("5.00")  # carried forward, not re-fetched
    assert prices[stale_addr] == Decimal("42.0")  # freshly fetched


@pytest.mark.integration
async def test_price_older_than_freshness_s_is_treated_as_stale(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """A price fetched longer ago than freshness_s is re-requested, not reused."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "3" * 36)
    addr = "0x" + "f6" * 20
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="OLD")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    # Default quote_refresh freshness_s is 3600s; 2 hours is well past that.
    await _insert_complete_quote_set(
        db_session,
        asset_id=asset_id,
        price_usd=Decimal("1.00"),
        fetched_at_sql="now() - interval '2 hours'",
    )
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    resolver_calls: list[list[str]] = []

    async def _fake_resolver(_session: AsyncSession, addresses: list[str]) -> dict[str, int]:
        resolver_calls.append(list(addresses))
        return {addr: _FRESH_CMC_ID}

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
        with patch("audr.jobs.quotes.resolve_cmc_ids", new=_fake_resolver):
            with respx.mock(assert_all_called=True) as mock_router:
                mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "data": [{"id": _FRESH_CMC_ID, "price": 99.0}],
                            "status": {"error_code": "0"},
                        },
                    )
                )
                await handle_quote_refresh(db_session, run_id)

    assert resolver_calls == [[addr]]
    prices = await _latest_quote_set_prices(db_session)
    assert prices[addr] == Decimal("99.0")


@pytest.mark.integration
async def test_all_fresh_skips_provider_call_entirely(db_session: AsyncSession) -> None:
    """When every held asset is already fresh, the provider is never called."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "4" * 36)
    addr = "0x" + "a7" * 20
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="FRESH2")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_complete_quote_set(
        db_session, asset_id=asset_id, price_usd=Decimal("7.50"), fetched_at_sql="now()"
    )
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    async def _unexpected_resolver(_session: AsyncSession, _addresses: list[str]) -> dict[str, int]:
        raise AssertionError("resolver should not be called when nothing is stale")

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
        with patch("audr.jobs.quotes.resolve_cmc_ids", new=_unexpected_resolver):
            with respx.mock(assert_all_called=False) as mock_router:
                mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                    return_value=httpx.Response(500)
                )
                await handle_quote_refresh(db_session, run_id)

    prices = await _latest_quote_set_prices(db_session)
    assert prices[addr] == Decimal("7.50")
