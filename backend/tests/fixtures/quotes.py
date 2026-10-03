"""Controlled CoinGecko quote HTTP fixtures for contract and integration tests (T015)."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
import respx
from httpx import Response

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def _price_response(coin_id: str, usd: Decimal) -> dict[str, Any]:
    return {coin_id: {"usd": float(usd)}}


@pytest.fixture()
def quotes_mock() -> Iterator[respx.MockRouter]:
    """HTTPX mock router pre-configured with default CoinGecko responses."""
    with respx.mock(assert_all_called=False) as mock:
        # Default: ETH at $2000
        mock.get(f"{_COINGECKO_BASE}/simple/price").mock(
            return_value=Response(
                200,
                json=_price_response("ethereum", Decimal("2000.00")),
            )
        )
        yield mock


class CoinGeckoStub:
    """Programmatic CoinGecko stub for fine-grained test control."""

    def __init__(
        self,
        router: respx.MockRouter,
        base_url: str = _COINGECKO_BASE,
    ) -> None:
        self._router = router
        self._base = base_url

    def set_price(self, coin_id: str, usd: Decimal) -> None:
        self._router.get(f"{self._base}/simple/price").mock(
            return_value=Response(200, json=_price_response(coin_id, usd))
        )

    def set_http_error(self, status_code: int) -> None:
        self._router.get(f"{self._base}/simple/price").mock(return_value=Response(status_code))

    def set_rate_limited(self) -> None:
        self.set_http_error(429)
