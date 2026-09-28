"""Contract tests for the CoinGecko Demo quote provider (T048 / AUD-61).

These tests verify the provider correctly parses, validates, and handles
CoinGecko API responses using respx HTTP mocks — no real network calls.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import respx
from httpx import Response

from audr.providers.coingecko_demo import (
    CoinGeckoError,
    CoinGeckoProvider,
    RateLimitError,
)

_BASE = "https://api.coingecko.com/api/v3"
_FAKE_KEY = "test-api-key"
_ETH_ADDR = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_USDC_ADDR = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _eth_price_response(usd: float) -> dict[str, Any]:
    return {"ethereum": {"usd": usd}}


def _token_price_response(address: str, usd: float) -> dict[str, Any]:
    return {address.lower(): {"usd": usd}}


@pytest.mark.contract
async def test_get_eth_price_parses_usd() -> None:
    """get_eth_price returns exact Decimal from CoinGecko /simple/price."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/price").mock(
            return_value=Response(200, json=_eth_price_response(2000.50))
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        price = await provider.get_eth_price()
        await provider.close()

    assert isinstance(price, Decimal)
    assert price == Decimal("2000.5")


@pytest.mark.contract
async def test_get_eth_price_sends_api_key_header() -> None:
    """Provider sends the x-cg-demo-api-key header."""
    with respx.mock() as mock:
        route = mock.get(f"{_BASE}/simple/price").mock(
            return_value=Response(200, json=_eth_price_response(1500.0))
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        await provider.get_eth_price()
        await provider.close()

    assert route.called
    assert route.calls.last.request.headers["x-cg-demo-api-key"] == _FAKE_KEY


@pytest.mark.contract
async def test_get_token_prices_returns_dict() -> None:
    """get_token_prices returns {lowercase_address: Decimal} for found tokens."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/token_price/ethereum").mock(
            return_value=Response(200, json=_token_price_response(_USDC_ADDR, 1.0))
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        prices = await provider.get_token_prices([_USDC_ADDR])
        await provider.close()

    assert _USDC_ADDR in prices
    assert prices[_USDC_ADDR] == Decimal("1.0")


@pytest.mark.contract
async def test_get_token_prices_empty_list_returns_empty() -> None:
    """Empty address list returns empty dict without making an HTTP call."""
    with respx.mock(assert_all_called=True):
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        prices = await provider.get_token_prices([])
        await provider.close()

    assert prices == {}


@pytest.mark.contract
async def test_rate_limit_raises_rate_limit_error() -> None:
    """HTTP 429 raises RateLimitError, a subclass of CoinGeckoError."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/price").mock(return_value=Response(429))
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        with pytest.raises(RateLimitError) as exc_info:
            await provider.get_eth_price()
        await provider.close()

    assert exc_info.value.status_code == 429
    assert isinstance(exc_info.value, CoinGeckoError)


@pytest.mark.contract
async def test_5xx_error_raises_coingecko_error() -> None:
    """HTTP 5xx raises CoinGeckoError."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/price").mock(return_value=Response(503))
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        with pytest.raises(CoinGeckoError) as exc_info:
            await provider.get_eth_price()
        await provider.close()

    assert exc_info.value.status_code == 503


@pytest.mark.contract
async def test_missing_coin_id_raises_coingecko_error() -> None:
    """Response missing the expected coin_id raises CoinGeckoError."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/price").mock(
            return_value=Response(200, json={"bitcoin": {"usd": 50000.0}})
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        with pytest.raises(CoinGeckoError):
            await provider.get_eth_price()
        await provider.close()


@pytest.mark.contract
async def test_get_prices_routes_eth_and_tokens() -> None:
    """get_prices fetches ETH via /simple/price and tokens via /simple/token_price."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/price").mock(
            return_value=Response(200, json=_eth_price_response(2000.0))
        )
        mock.get(f"{_BASE}/simple/token_price/ethereum").mock(
            return_value=Response(200, json=_token_price_response(_USDC_ADDR, 1.0))
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        prices = await provider.get_prices([_ETH_ADDR, _USDC_ADDR])
        await provider.close()

    assert prices[_ETH_ADDR] == Decimal("2000.0")
    assert prices[_USDC_ADDR] == Decimal("1.0")


@pytest.mark.contract
async def test_get_token_prices_unknown_token_omitted() -> None:
    """Tokens not in the CoinGecko response are silently omitted (unknown ≠ zero)."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/token_price/ethereum").mock(
            return_value=Response(200, json={})
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        prices = await provider.get_token_prices([_USDC_ADDR])
        await provider.close()

    # Token not found → not in results (never coerced to zero)
    assert _USDC_ADDR not in prices
    assert prices == {}


@pytest.mark.contract
async def test_get_token_prices_normalises_address_to_lowercase() -> None:
    """Prices dict keys are always lowercase regardless of CoinGecko response case."""
    mixed_case = _USDC_ADDR.upper()
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/token_price/ethereum").mock(
            return_value=Response(200, json=_token_price_response(mixed_case, 1.0))
        )
        provider = CoinGeckoProvider(api_key=_FAKE_KEY)
        prices = await provider.get_token_prices([mixed_case])
        await provider.close()

    assert all(k == k.lower() for k in prices)


@pytest.mark.contract
async def test_context_manager_closes_client() -> None:
    """Provider used as async context manager closes its HTTP client cleanly."""
    with respx.mock() as mock:
        mock.get(f"{_BASE}/simple/price").mock(
            return_value=Response(200, json=_eth_price_response(2000.0))
        )
        async with CoinGeckoProvider(api_key=_FAKE_KEY) as provider:
            price = await provider.get_eth_price()

    assert price == Decimal("2000.0")
