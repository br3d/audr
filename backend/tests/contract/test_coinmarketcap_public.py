"""Contract tests for the CoinMarketCap keyless quote provider (AUD-358).

These tests verify the provider correctly parses, validates, and handles
CoinMarketCap API responses using respx HTTP mocks — no real network calls.
Modelled on tests/contract/test_coingecko_demo.py.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response

from audr.jobs.policy import RetryPolicy
from audr.providers.coinmarketcap_public import (
    CoinMarketCapError,
    CoinMarketCapProvider,
    RateLimitError,
)

_FAST_RETRY_POLICY = RetryPolicy(base_delay_s=0.0, max_delay_s=0.0, jitter=False)

_BASE = "https://pro-api.coinmarketcap.com"
_ETH_ADDR = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_USDC_ADDR = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
_USDC_CMC_ID = 3408
_ETH_CMC_ID = 1027


def _simple_price_response(entries: list[tuple[int, float]]) -> dict[str, Any]:
    return {
        "data": [{"id": cmc_id, "price": price} for cmc_id, price in entries],
        "status": {"error_code": "0"},
    }


def _resolver_for(mapping: dict[str, int]):
    async def _resolve(addresses: list[str]) -> dict[str, int]:
        return {a: mapping[a] for a in addresses if a in mapping}

    return _resolve


@pytest.mark.contract
async def test_get_prices_by_ids_parses_batched_response() -> None:
    """A single request prices every requested id."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(
                200, json=_simple_price_response([(1, 83528.19), (1027, 2690.18)])
            )
        )
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        prices = await provider.get_prices_by_ids([1, 1027])
        await provider.close()

    assert prices[1] == Decimal("83528.19")
    assert prices[1027] == Decimal("2690.18")


@pytest.mark.contract
async def test_get_prices_by_ids_empty_list_returns_empty() -> None:
    """Empty id list returns empty dict without making an HTTP call."""
    with respx.mock(assert_all_called=True):
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        prices = await provider.get_prices_by_ids([])
        await provider.close()

    assert prices == {}


@pytest.mark.contract
async def test_get_prices_by_ids_batches_into_one_request() -> None:
    """Multiple ids are sent as a single comma-joined request, not N requests."""
    with respx.mock() as mock:
        route = mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(200, json=_simple_price_response([(1, 1.0), (2, 2.0), (3, 3.0)]))
        )
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        await provider.get_prices_by_ids([1, 2, 3])
        await provider.close()

    assert route.call_count == 1
    assert route.calls.last.request.url.params["ids"] == "1,2,3"


@pytest.mark.contract
async def test_rate_limit_raises_rate_limit_error() -> None:
    """HTTP 429 raises RateLimitError, a subclass of CoinMarketCapError."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(return_value=Response(429))
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        with pytest.raises(RateLimitError) as exc_info:
            await provider.get_prices_by_ids([1])
        await provider.close()

    assert exc_info.value.status_code == 429
    assert isinstance(exc_info.value, CoinMarketCapError)


@pytest.mark.contract
async def test_5xx_error_raises_coinmarketcap_error() -> None:
    """HTTP 5xx raises CoinMarketCapError."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(return_value=Response(503))
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        with pytest.raises(CoinMarketCapError) as exc_info:
            await provider.get_prices_by_ids([1])
        await provider.close()

    assert exc_info.value.status_code == 503


@pytest.mark.contract
async def test_get_prices_by_ids_unknown_id_omitted() -> None:
    """Ids missing from the response are silently omitted (unknown ≠ zero)."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(200, json=_simple_price_response([]))
        )
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        prices = await provider.get_prices_by_ids([_USDC_CMC_ID])
        await provider.close()

    assert _USDC_CMC_ID not in prices
    assert prices == {}


@pytest.mark.contract
async def test_get_prices_routes_eth_and_resolved_tokens() -> None:
    """get_prices resolves ERC-20 addresses via the injected resolver, fetches
    everything (including native ETH) in one batched request, and maps
    prices back onto the original lowercase addresses."""
    with respx.mock() as mock:
        route = mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(
                200, json=_simple_price_response([(_ETH_CMC_ID, 2000.0), (_USDC_CMC_ID, 1.0)])
            )
        )
        provider = CoinMarketCapProvider(resolver=_resolver_for({_USDC_ADDR: _USDC_CMC_ID}))
        prices = await provider.get_prices([_ETH_ADDR, _USDC_ADDR])
        await provider.close()

    assert route.call_count == 1
    assert prices[_ETH_ADDR] == Decimal("2000.0")
    assert prices[_USDC_ADDR] == Decimal("1.0")


@pytest.mark.contract
async def test_get_prices_unresolved_address_omitted() -> None:
    """An address the resolver can't map is absent from the result, not zero."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(200, json=_simple_price_response([]))
        )
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        prices = await provider.get_prices([_USDC_ADDR])
        await provider.close()

    assert _USDC_ADDR not in prices
    assert prices == {}


@pytest.mark.contract
async def test_get_prices_preserves_full_decimal_precision() -> None:
    """Prices with many significant digits are stored exactly, not rounded through float."""
    raw = b'{"data": [{"id": 1027, "price": 1234.567890123456789}], "status": {"error_code": "0"}}'
    expected = Decimal("1234.567890123456789")
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(200, content=raw, headers={"content-type": "application/json"})
        )
        async with CoinMarketCapProvider(resolver=_resolver_for({})) as provider:
            price = await provider.get_eth_price()

    assert price == expected


@pytest.mark.contract
async def test_fetch_map_page_returns_rows() -> None:
    """fetch_map_page returns the raw `data` rows from /cryptocurrency/map."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/cryptocurrency/map").mock(
            return_value=Response(
                200,
                json={
                    "data": [
                        {"id": 1, "symbol": "BTC", "platform": None},
                        {
                            "id": 3408,
                            "symbol": "USDC",
                            "platform": {"id": 183, "token_address": "0xdeadbeef"},
                        },
                    ],
                    "status": {"error_code": "0"},
                },
            )
        )
        provider = CoinMarketCapProvider(resolver=_resolver_for({}))
        rows = await provider.fetch_map_page(start=1, limit=5000)
        await provider.close()

    assert len(rows) == 2
    assert rows[0]["symbol"] == "BTC"


@pytest.mark.contract
async def test_context_manager_closes_client() -> None:
    """Provider used as async context manager closes its HTTP client cleanly."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(200, json=_simple_price_response([(_ETH_CMC_ID, 2000.0)]))
        )
        async with CoinMarketCapProvider(resolver=_resolver_for({})) as provider:
            price = await provider.get_eth_price()

    assert price == Decimal("2000.0")


@pytest.mark.contract
async def test_get_prices_by_ids_chunks_at_fifty_ids() -> None:
    """More than 50 ids are split across requests (AUD-359).

    The keyless endpoint answers any `ids` list longer than 50 with HTTP 400
    ("'ids' parameter is currently restricted to 50 values"), which failed every
    quote_refresh on staging once the portfolio resolved past 50 assets. Each
    chunk must therefore carry at most 50 ids, and prices from every chunk must
    be merged into one result.
    """
    requested_ids: list[list[str]] = []
    all_ids = list(range(1, 121))  # 120 ids -> 3 chunks of 50/50/20

    def _respond(request: Any) -> Response:
        ids = request.url.params["ids"].split(",")
        requested_ids.append(ids)
        return _simple_price_response_for(ids)

    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(side_effect=_respond)
        async with CoinMarketCapProvider(resolver=_resolver_for({})) as provider:
            prices = await provider.get_prices_by_ids(all_ids)

    assert [len(chunk) for chunk in requested_ids] == [50, 50, 20]
    assert all(len(chunk) <= 50 for chunk in requested_ids)
    # Every id was asked for exactly once, and every price made it back.
    assert sorted(int(i) for chunk in requested_ids for i in chunk) == all_ids
    assert len(prices) == len(all_ids)
    assert prices[1] == Decimal("1.5")
    assert prices[120] == Decimal("180.0")


def _simple_price_response_for(ids: list[str]) -> Response:
    return Response(
        200,
        json={
            "data": [{"id": int(i), "price": float(int(i) * 1.5)} for i in ids],
            "status": {"error_code": "0"},
        },
    )


@pytest.mark.contract
async def test_get_prices_by_ids_retries_on_rate_limit_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that trips the anonymous rate limit is retried with exponential
    backoff + jitter (AUD-370), not just once, until it succeeds or the retry
    policy is exhausted."""
    delays: list[float] = []
    monkeypatch.setattr(
        "audr.providers.coinmarketcap_public.asyncio.sleep",
        AsyncMock(side_effect=lambda d: delays.append(d)),
    )
    attempts = {"n": 0}

    def _respond(request: Any) -> Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return Response(429, json={"status": {"error_code": "1008"}})
        return Response(200, json=_simple_price_response([(1027, 2690.18)]))

    policy = RetryPolicy(base_delay_s=1.0, max_delay_s=60.0, jitter=False)
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(side_effect=_respond)
        async with CoinMarketCapProvider(
            resolver=_resolver_for({}), retry_policy=policy
        ) as provider:
            prices = await provider.get_prices_by_ids([1027])

    assert attempts["n"] == 3
    assert delays == [1.0, 2.0]  # exponential backoff between retries
    assert prices[1027] == Decimal("2690.18")


@pytest.mark.contract
async def test_get_prices_by_ids_raises_if_rate_limit_persists() -> None:
    """429s that outlast the retry policy propagate, leaving pacing to the
    worker's own retry backoff."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(
            return_value=Response(429, json={"status": {"error_code": "1008"}})
        )
        async with CoinMarketCapProvider(
            resolver=_resolver_for({}), retry_policy=_FAST_RETRY_POLICY
        ) as provider:
            with pytest.raises(RateLimitError):
                await provider.get_prices_by_ids([1027])


@pytest.mark.contract
async def test_get_prices_acquires_rate_limiter_before_each_request() -> None:
    """Every HTTP attempt, including retries, draws from the injected rate limiter."""
    acquire_calls = {"n": 0}

    class _FakeLimiter:
        async def acquire(self) -> None:
            acquire_calls["n"] += 1

    attempts = {"n": 0}

    def _respond(request: Any) -> Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return Response(429, json={"status": {"error_code": "1008"}})
        return Response(200, json=_simple_price_response([(1027, 2690.18)]))

    with respx.mock() as mock:
        mock.get(f"{_BASE}/public-api/v1/simple/price").mock(side_effect=_respond)
        async with CoinMarketCapProvider(
            resolver=_resolver_for({}),
            rate_limiter=_FakeLimiter(),
            retry_policy=_FAST_RETRY_POLICY,
        ) as provider:
            await provider.get_prices_by_ids([1027])

    assert acquire_calls["n"] == 2
