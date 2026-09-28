"""CoinGecko Demo API quote provider (T052 / US2 / AUD-65).

Supports:
- ERC-20 token prices via /simple/token_price/{platform_id}
- Native ETH price via /simple/price?ids=ethereum

Rate limits: CoinGecko Demo allows ~30 req/min.  Callers should avoid
calling this more frequently than once per minute.

Never logs or exposes the API key.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.coingecko.com/api/v3"
_ETH_NATIVE_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_ETH_COIN_ID = "ethereum"
_PLATFORM_ID = "ethereum"
_TIMEOUT = 15.0


class CoinGeckoError(Exception):
    """Raised for non-2xx HTTP responses from CoinGecko."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(f"CoinGecko HTTP {status_code}: {message}")
        self.status_code = status_code


class RateLimitError(CoinGeckoError):
    """Raised when CoinGecko returns HTTP 429."""


class CoinGeckoProvider:
    """Read-only CoinGecko Demo quote adapter.

    Can be used as an async context manager or with an externally managed
    httpx.AsyncClient (pass *http_client* to reuse connections in tests).
    """

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = _BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT),
            follow_redirects=False,
        )

    async def __aenter__(self) -> "CoinGeckoProvider":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    async def get_eth_price(self) -> Decimal:
        """Return the current ETH price in USD."""
        data = await self._get(
            "/simple/price",
            params={
                "ids": _ETH_COIN_ID,
                "vs_currencies": "usd",
                "precision": "18",
            },
        )
        return _extract_simple_price(data, _ETH_COIN_ID)

    async def get_token_prices(
        self,
        contract_addresses: list[str],
        *,
        platform_id: str = _PLATFORM_ID,
    ) -> dict[str, Decimal]:
        """Return {lowercase_address: price_usd} for recognised tokens.

        Addresses not found in the CoinGecko catalog are silently omitted —
        callers must treat absent tokens as unknown, not zero.
        """
        if not contract_addresses:
            return {}
        addresses_param = ",".join(a.lower() for a in contract_addresses)
        data = await self._get(
            f"/simple/token_price/{platform_id}",
            params={
                "contract_addresses": addresses_param,
                "vs_currencies": "usd",
                "precision": "18",
            },
        )
        result: dict[str, Decimal] = {}
        for addr, prices in data.items():
            if not isinstance(prices, dict):
                continue
            usd_val = prices.get("usd")
            if usd_val is None:
                continue
            try:
                result[addr.lower()] = Decimal(str(usd_val))
            except Exception:
                logger.warning("coingecko: unparseable price for %s: %r", addr, usd_val)
        return result

    async def get_prices(
        self,
        token_addresses: list[str],
        *,
        include_eth: bool = False,
    ) -> dict[str, Decimal]:
        """Convenience: fetch prices for an arbitrary mix of tokens and ETH.

        Separates the ETH native placeholder from ERC-20 contract addresses,
        fetches each batch via the appropriate endpoint, and merges the results.
        """
        prices: dict[str, Decimal] = {}

        erc20 = [a for a in token_addresses if a.lower() != _ETH_NATIVE_ADDRESS]
        wants_eth = include_eth or any(
            a.lower() == _ETH_NATIVE_ADDRESS for a in token_addresses
        )

        if erc20:
            token_prices = await self.get_token_prices(erc20)
            prices.update(token_prices)

        if wants_eth:
            eth_price = await self.get_eth_price()
            prices[_ETH_NATIVE_ADDRESS] = eth_price

        return prices

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self._base}{path}"
        response = await self._client.get(
            url,
            params=params,
            headers={"x-cg-demo-api-key": self._api_key},
        )
        _check_response(response)
        return json.loads(response.text, parse_float=Decimal)  # type: ignore[no-any-return]


def _check_response(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise RateLimitError(429, "rate limited")
    if not response.is_success:
        raise CoinGeckoError(response.status_code)


def _extract_simple_price(data: dict[str, Any], coin_id: str) -> Decimal:
    """Parse a /simple/price response and return the USD price."""
    entry = data.get(coin_id)
    if not isinstance(entry, dict):
        raise CoinGeckoError(0, f"coin_id '{coin_id}' not found in response")
    usd_val = entry.get("usd")
    if usd_val is None:
        raise CoinGeckoError(0, f"'usd' key missing for coin_id '{coin_id}'")
    try:
        return Decimal(str(usd_val))
    except Exception as exc:
        raise CoinGeckoError(0, f"unparseable USD value: {usd_val!r}") from exc
