"""CoinMarketCap keyless public-API quote provider (AUD-358).

Unlike CoinGecko Demo, CoinMarketCap's `/public-api/v1/*` endpoints work
without an API key — this is what lets `quote_refresh` price a portfolio
out of the box on a fresh install with zero integrations configured.

The endpoint only accepts numeric CoinMarketCap ids, not contract addresses
(there is no keyless address-lookup endpoint), so callers must resolve
addresses to ids first — see `audr.assets.cmc_catalog.resolve_cmc_ids`.
`get_prices` takes a *resolver* callable to do that, so this provider has
no direct DB dependency and stays unit-testable like CoinGeckoProvider.

Anonymous rate limits are real (observed HTTP 429 on back-to-back calls) —
callers must not retry in a tight loop; the job worker's retry backoff
(AUD-356) is what paces re-attempts after a RateLimitError.

Never logs or exposes request/response bodies beyond what's needed for
debugging HTTP failures (there's no secret here, but keep the habit).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://pro-api.coinmarketcap.com"
_ETH_NATIVE_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_ETH_CMC_ID = 1027
_TIMEOUT = 15.0

# The keyless endpoint caps `ids` at 50 values per request and answers a larger
# list with HTTP 400 "'ids' parameter is currently restricted to 50 values"
# (AUD-359). A real portfolio resolves to a few hundred ids, so requests must be
# chunked — sending them all at once failed every quote_refresh on staging.
_MAX_IDS_PER_REQUEST = 50
# Anonymous calls are rate limited aggressively, so chunks are paced rather than
# fired back to back. A chunk that still trips the limit is retried once after a
# longer pause; if it trips again the RateLimitError propagates and the worker's
# retry backoff (AUD-356) decides when to re-attempt the whole job.
_INTER_BATCH_DELAY_S = 1.5
_RATE_LIMIT_RETRY_DELAY_S = 8.0

# AddressResolver maps a list of lowercase ERC-20 contract addresses to
# {address: cmc_id} for whichever addresses it can resolve. Addresses it
# cannot resolve are simply absent from the result — never guessed.
AddressResolver = Callable[[list[str]], Awaitable[dict[str, int]]]


class CoinMarketCapError(Exception):
    """Raised for non-2xx HTTP responses from the CoinMarketCap public API."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(f"CoinMarketCap HTTP {status_code}: {message}")
        self.status_code = status_code


class RateLimitError(CoinMarketCapError):
    """Raised when CoinMarketCap returns HTTP 429 (anonymous rate limit)."""


class CoinMarketCapProvider:
    """Read-only CoinMarketCap public (keyless) quote adapter.

    Can be used as an async context manager or with an externally managed
    httpx.AsyncClient (pass *http_client* to reuse connections in tests).
    """

    def __init__(
        self,
        *,
        resolver: AddressResolver,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = _BASE_URL,
    ) -> None:
        self._resolver = resolver
        self._base = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT),
            follow_redirects=False,
        )

    async def __aenter__(self) -> "CoinMarketCapProvider":
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
        """Return the current native ETH price in USD."""
        prices = await self.get_prices_by_ids([_ETH_CMC_ID])
        price = prices.get(_ETH_CMC_ID)
        if price is None:
            raise CoinMarketCapError(0, f"cmc id {_ETH_CMC_ID} not found in response")
        return price

    async def get_prices_by_ids(self, cmc_ids: list[int]) -> dict[int, Decimal]:
        """Return {cmc_id: price_usd} for recognised ids.

        Ids are requested in chunks of at most ``_MAX_IDS_PER_REQUEST`` because
        the endpoint rejects longer lists outright (AUD-359), and chunks are
        paced to stay under the anonymous rate limit. Ids not found in the
        response are silently omitted — callers must treat an absent id as
        unknown, never zero.
        """
        if not cmc_ids:
            return {}
        unique_ids = sorted(set(cmc_ids))
        result: dict[int, Decimal] = {}
        for position in range(0, len(unique_ids), _MAX_IDS_PER_REQUEST):
            chunk = unique_ids[position : position + _MAX_IDS_PER_REQUEST]
            if position:
                await asyncio.sleep(_INTER_BATCH_DELAY_S)
            data = await self._get_price_chunk(chunk)
            self._collect_prices(data, result)
        return result

    async def _get_price_chunk(self, chunk: list[int]) -> dict[str, Any]:
        """Fetch one chunk of ids, retrying once if the rate limit trips."""
        params = {"ids": ",".join(str(i) for i in chunk), "convert": "USD"}
        try:
            return await self._get("/public-api/v1/simple/price", params=params)
        except RateLimitError:
            logger.info(
                "coinmarketcap: rate limited on a chunk of %d ids, retrying once in %.1fs",
                len(chunk),
                _RATE_LIMIT_RETRY_DELAY_S,
            )
            await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY_S)
            return await self._get("/public-api/v1/simple/price", params=params)

    @staticmethod
    def _collect_prices(data: dict[str, Any], into: dict[int, Decimal]) -> None:
        for entry in data.get("data", []):
            if not isinstance(entry, dict):
                continue
            cmc_id = entry.get("id")
            price = entry.get("price")
            if cmc_id is None or price is None:
                continue
            try:
                into[int(cmc_id)] = Decimal(str(price))
            except Exception:
                logger.warning("coinmarketcap: unparseable price for id %r: %r", cmc_id, price)

    async def get_prices(
        self,
        token_addresses: list[str],
        *,
        include_eth: bool = False,
    ) -> dict[str, Decimal]:
        """Convenience: fetch prices for an arbitrary mix of tokens and ETH.

        Resolves ERC-20 addresses to CoinMarketCap ids via the injected
        resolver, fetches every id in chunked requests, and maps
        prices back onto the original lowercase addresses. An address that
        the resolver cannot map (no pin, no address match, ambiguous
        symbol) is simply absent from the result, matching
        CoinGeckoProvider.get_prices' unknown-token contract.
        """
        erc20 = [a.lower() for a in token_addresses if a.lower() != _ETH_NATIVE_ADDRESS]
        wants_eth = include_eth or any(
            a.lower() == _ETH_NATIVE_ADDRESS for a in token_addresses
        )

        resolved: dict[str, int] = await self._resolver(erc20) if erc20 else {}

        ids_wanted = set(resolved.values())
        if wants_eth:
            ids_wanted.add(_ETH_CMC_ID)

        prices_by_id = await self.get_prices_by_ids(list(ids_wanted)) if ids_wanted else {}

        prices: dict[str, Decimal] = {}
        for address, cmc_id in resolved.items():
            price = prices_by_id.get(cmc_id)
            if price is not None:
                prices[address] = price

        if wants_eth:
            eth_price = prices_by_id.get(_ETH_CMC_ID)
            if eth_price is not None:
                prices[_ETH_NATIVE_ADDRESS] = eth_price

        return prices

    async def fetch_map_page(self, *, start: int, limit: int) -> list[dict[str, Any]]:
        """Return one page of the keyless `/cryptocurrency/map` catalog.

        Used by `audr.assets.cmc_catalog` to refresh the address/symbol
        resolution table. Callers paginate with `start` (1-based) until a
        page returns fewer than `limit` rows.
        """
        data = await self._get(
            "/public-api/v1/cryptocurrency/map",
            params={"start": str(start), "limit": str(limit)},
        )
        rows = data.get("data", [])
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self._base}{path}"
        response = await self._client.get(url, params=params)
        _check_response(response)
        return json.loads(response.text, parse_float=Decimal)  # type: ignore[no-any-return]


def _check_response(response: httpx.Response) -> None:
    if response.status_code == 429:
        raise RateLimitError(429, "anonymous rate limit reached")
    if not response.is_success:
        raise CoinMarketCapError(response.status_code)
