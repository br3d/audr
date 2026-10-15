"""Keyless CoinGecko residual fallback in quote_refresh (AUD-558).

CoinMarketCap's keyless `/public-api/v1/simple/price` is the primary
provider out of the box and prices most held assets in one batch, but it
has no address-lookup endpoint — an asset the resolver can't map to a CMC
id is never even requested. CoinGecko's address-based `/simple/token_price`
works without a key too, just capped at one contract address per request,
so after the primary pass a residual pass asks keyless CoinGecko about
whatever held assets are still unpriced, one address at a time.

Marker: integration
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import audr.jobs.quotes as quotes_module
from audr.jobs.quotes import handle_quote_refresh
from tests.helpers import wallet_address_columns

pytestmark = pytest.mark.integration

_CMC_BASE = "https://pro-api.coinmarketcap.com"
_CG_BASE = "https://api.coingecko.com/api/v3"
_PRICED_CMC_ID = 2001


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


async def _setup_fencing(
    session: AsyncSession, run_id: uuid.UUID, *, with_coingecko_integration: bool = False
) -> None:
    await session.execute(
        text(
            "INSERT INTO job_run (id, kind, status, max_retries)"
            " VALUES (:id, 'quote_refresh', 'in_progress', 3)"
        ),
        {"id": str(run_id)},
    )
    if with_coingecko_integration:
        await session.execute(
            text(
                "INSERT INTO integration (kind, encrypted_blob)"
                " VALUES ('coingecko', :blob) ON CONFLICT (kind) DO NOTHING"
            ),
            {"blob": b"\x00"},
        )


async def _latest_quote_set_row(session: AsyncSession) -> tuple[str, str]:
    row = (
        await session.execute(
            text("SELECT id, status FROM quote_set ORDER BY created_at DESC LIMIT 1")
        )
    ).first()
    assert row is not None
    return str(row[0]), str(row[1])


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


def _cmc_response(cmc_id: int, price: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": [{"id": cmc_id, "price": price}], "status": {"error_code": "0"}},
    )


@pytest.mark.integration
async def test_residual_fires_only_for_unpriced_held_assets(db_session: AsyncSession) -> None:
    """CoinGecko is only asked about the address CMC's resolver couldn't map."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "5" * 36)
    priced_addr = "0x" + "a1" * 20
    gap_addr = "0x" + "d0" * 20
    priced_id = await _insert_asset(db_session, token_address=priced_addr, symbol="PRICED")
    gap_id = await _insert_asset(db_session, token_address=gap_addr, symbol="GAP")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=priced_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=gap_id)
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    async def _fake_resolver(_session: AsyncSession, addresses: list[str]) -> dict[str, int]:
        # Only the priced address resolves to a CMC id — gap_addr is a token
        # CoinMarketCap's keyless map has never heard of.
        return {a: _PRICED_CMC_ID for a in addresses if a == priced_addr}

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
        with patch("audr.jobs.quotes.resolve_cmc_ids", new=_fake_resolver):
            with respx.mock(assert_all_called=True) as mock_router:
                mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                    return_value=_cmc_response(_PRICED_CMC_ID, 10.0)
                )
                cg_route = mock_router.get(f"{_CG_BASE}/simple/token_price/ethereum").mock(
                    return_value=httpx.Response(200, json={gap_addr: {"usd": 0.5}})
                )
                await handle_quote_refresh(db_session, run_id)

    assert cg_route.call_count == 1
    sent_addr = cg_route.calls.last.request.url.params["contract_addresses"]
    assert sent_addr == gap_addr

    prices = await _latest_quote_set_prices(db_session)
    assert prices[priced_addr] == Decimal("10.0")
    assert prices[gap_addr] == Decimal("0.5")


@pytest.mark.integration
async def test_residual_chunks_unpriced_addresses_to_one_per_request(
    db_session: AsyncSession,
) -> None:
    """Two residual gaps become two separate single-address CoinGecko requests."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "6" * 36)
    gap_a = "0x" + "d1" * 20
    gap_b = "0x" + "d2" * 20
    gap_a_id = await _insert_asset(db_session, token_address=gap_a, symbol="GAPA")
    gap_b_id = await _insert_asset(db_session, token_address=gap_b, symbol="GAPB")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=gap_a_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=gap_b_id)
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    async def _empty_resolver(_session: AsyncSession, _addresses: list[str]) -> dict[str, int]:
        return {}

    def _cg_side_effect(request: httpx.Request) -> httpx.Response:
        addr = request.url.params["contract_addresses"]
        assert "," not in addr, "keyless residual lookups must send one address per request"
        return httpx.Response(200, json={addr: {"usd": 1.0}})

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
        with patch("audr.jobs.quotes.resolve_cmc_ids", new=_empty_resolver):
            with respx.mock(assert_all_called=False) as mock_router:
                mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                    return_value=httpx.Response(
                        200, json={"data": [], "status": {"error_code": "0"}}
                    )
                )
                cg_route = mock_router.get(f"{_CG_BASE}/simple/token_price/ethereum").mock(
                    side_effect=_cg_side_effect
                )
                await handle_quote_refresh(db_session, run_id)

    assert cg_route.call_count == 2
    prices = await _latest_quote_set_prices(db_session)
    assert prices[gap_a] == Decimal("1.0")
    assert prices[gap_b] == Decimal("1.0")


@pytest.mark.integration
async def test_residual_cap_truncates_and_warns(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """More residual gaps than the cap allows truncates the list and logs a warning."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "7" * 36)
    gap_a = "0x" + "e1" * 20
    gap_b = "0x" + "e2" * 20
    gap_a_id = await _insert_asset(db_session, token_address=gap_a, symbol="CAPA")
    gap_b_id = await _insert_asset(db_session, token_address=gap_b, symbol="CAPB")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=gap_a_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=gap_b_id)
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    async def _empty_resolver(_session: AsyncSession, _addresses: list[str]) -> dict[str, int]:
        return {}

    with patch.object(quotes_module, "_MAX_RESIDUAL_LOOKUPS", 1):
        with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
            with patch("audr.jobs.quotes.resolve_cmc_ids", new=_empty_resolver):
                with respx.mock(assert_all_called=False) as mock_router:
                    mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                        return_value=httpx.Response(
                            200, json={"data": [], "status": {"error_code": "0"}}
                        )
                    )
                    cg_route = mock_router.get(f"{_CG_BASE}/simple/token_price/ethereum").mock(
                        side_effect=lambda request: httpx.Response(
                            200,
                            json={
                                request.url.params["contract_addresses"]: {"usd": 1.0},
                            },
                        )
                    )
                    with caplog.at_level(logging.WARNING, logger="audr.jobs.quotes"):
                        await handle_quote_refresh(db_session, run_id)

    assert cg_route.call_count == 1
    assert any(
        "residual cap reached" in record.getMessage() and "skipped=1" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.integration
async def test_residual_http_error_leaves_asset_unpriced_run_not_failed(
    db_session: AsyncSession,
) -> None:
    """A failed residual lookup is caught: the set stays complete, run doesn't raise."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "8" * 36)
    priced_addr = "0x" + "a9" * 20
    gap_addr = "0x" + "d9" * 20
    priced_id = await _insert_asset(db_session, token_address=priced_addr, symbol="OK")
    gap_id = await _insert_asset(db_session, token_address=gap_addr, symbol="BAD")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=priced_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=gap_id)
    await _setup_fencing(db_session, run_id)
    await db_session.flush()

    async def _fake_resolver(_session: AsyncSession, addresses: list[str]) -> dict[str, int]:
        return {a: _PRICED_CMC_ID for a in addresses if a == priced_addr}

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value=None)):
        with patch("audr.jobs.quotes.resolve_cmc_ids", new=_fake_resolver):
            with respx.mock(assert_all_called=True) as mock_router:
                mock_router.get(f"{_CMC_BASE}/public-api/v1/simple/price").mock(
                    return_value=_cmc_response(_PRICED_CMC_ID, 10.0)
                )
                mock_router.get(f"{_CG_BASE}/simple/token_price/ethereum").mock(
                    return_value=httpx.Response(500)
                )
                # Must not raise — a residual failure degrades, never fails the run.
                await handle_quote_refresh(db_session, run_id)

    quote_set_id, status = await _latest_quote_set_row(db_session)
    assert status == "complete"

    prices = await _latest_quote_set_prices(db_session)
    assert prices[priced_addr] == Decimal("10.0")
    assert gap_addr not in prices


@pytest.mark.integration
async def test_keyed_install_batches_and_skips_residual_when_nothing_unpriced(
    db_session: AsyncSession,
) -> None:
    """A keyed CoinGecko install prices everything in one batch; no residual call follows."""
    run_id = uuid.uuid4()
    wallet_id = await _insert_wallet(db_session, "0xface" + "9" * 36)
    addr_a = "0x" + "f1" * 20
    addr_b = "0x" + "f2" * 20
    asset_a = await _insert_asset(db_session, token_address=addr_a, symbol="KEYA")
    asset_b = await _insert_asset(db_session, token_address=addr_b, symbol="KEYB")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_a)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_b)
    await _setup_fencing(db_session, run_id, with_coingecko_integration=True)
    await db_session.flush()

    with patch("audr.jobs.quotes.get_coingecko_api_key", new=AsyncMock(return_value="fake-key")):
        with respx.mock(assert_all_called=True) as mock_router:
            cg_route = mock_router.get(f"{_CG_BASE}/simple/token_price/ethereum").mock(
                return_value=httpx.Response(200, json={addr_a: {"usd": 3.0}, addr_b: {"usd": 4.0}})
            )
            await handle_quote_refresh(db_session, run_id)

    # One batched request only — no second (residual) request was made.
    assert cg_route.call_count == 1
    sent = cg_route.calls.last.request.url.params["contract_addresses"]
    assert "," in sent

    prices = await _latest_quote_set_prices(db_session)
    assert prices[addr_a] == Decimal("3.0")
    assert prices[addr_b] == Decimal("4.0")
