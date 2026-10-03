"""Contract tests for the keyless asset icon provider (AUD-385).

Modelled on tests/contract/test_coingecko_demo.py / test_coinmarketcap_public.py —
respx HTTP mocks only, no real network calls.
"""

from __future__ import annotations

import pytest
import respx
from httpx import AsyncClient, Response

from audr.jobs.policy import RateLimiter
from audr.providers.asset_icons import (
    IconFetchError,
    IconRateLimitedError,
    fetch_coingecko_icon,
    fetch_trust_wallet_icon,
    trust_wallet_logo_url,
)

_NATIVE_ETH = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_USDC_LOWER = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
_USDC_CHECKSUM = "0xA0b86991c6218b36c1d19D4A2e9Eb0cE3606eB48"
_TRUST_WALLET_BASE = (
    "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ethereum"
)
_COINGECKO_BASE = "https://api.coingecko.com/api/v3"

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _fast_rate_limiter() -> RateLimiter:
    return RateLimiter(calls_per_second=1000.0, burst=10)


# ---------------------------------------------------------------------------
# trust_wallet_logo_url
# ---------------------------------------------------------------------------


def test_trust_wallet_logo_url_checksums_contract_address() -> None:
    url = trust_wallet_logo_url(_USDC_LOWER)
    assert url == f"{_TRUST_WALLET_BASE}/assets/{_USDC_CHECKSUM}/logo.png"


def test_trust_wallet_logo_url_native_eth_uses_info_path() -> None:
    url = trust_wallet_logo_url(_NATIVE_ETH)
    assert url == f"{_TRUST_WALLET_BASE}/info/logo.png"


# ---------------------------------------------------------------------------
# fetch_trust_wallet_icon
# ---------------------------------------------------------------------------


@pytest.mark.contract
async def test_fetch_trust_wallet_icon_success() -> None:
    url = trust_wallet_logo_url(_USDC_LOWER)
    with respx.mock() as mock:
        mock.get(url).mock(
            return_value=Response(200, content=_PNG_BYTES, headers={"content-type": "image/png"})
        )
        async with AsyncClient() as client:
            icon = await fetch_trust_wallet_icon(client, _USDC_LOWER)

    assert icon is not None
    assert icon.content_type == "image/png"
    assert icon.data == _PNG_BYTES
    assert icon.source == "trustwallet"


@pytest.mark.contract
async def test_fetch_trust_wallet_icon_404_returns_none() -> None:
    url = trust_wallet_logo_url(_USDC_LOWER)
    with respx.mock() as mock:
        mock.get(url).mock(return_value=Response(404))
        async with AsyncClient() as client:
            icon = await fetch_trust_wallet_icon(client, _USDC_LOWER)

    assert icon is None


@pytest.mark.contract
async def test_fetch_trust_wallet_icon_rejects_disallowed_content_type() -> None:
    url = trust_wallet_logo_url(_USDC_LOWER)
    with respx.mock() as mock:
        mock.get(url).mock(
            return_value=Response(
                200, content=b"<html></html>", headers={"content-type": "text/html"}
            )
        )
        async with AsyncClient() as client:
            icon = await fetch_trust_wallet_icon(client, _USDC_LOWER)

    # Disallowed content-type is a fetch error internally, swallowed to None
    # so the caller falls through to the next keyless source.
    assert icon is None


@pytest.mark.contract
async def test_fetch_trust_wallet_icon_rejects_oversize_response() -> None:
    url = trust_wallet_logo_url(_USDC_LOWER)
    oversized = b"\x00" * (256 * 1024 + 1)
    with respx.mock() as mock:
        mock.get(url).mock(
            return_value=Response(200, content=oversized, headers={"content-type": "image/png"})
        )
        async with AsyncClient() as client:
            icon = await fetch_trust_wallet_icon(client, _USDC_LOWER)

    assert icon is None


# ---------------------------------------------------------------------------
# fetch_coingecko_icon
# ---------------------------------------------------------------------------


@pytest.mark.contract
async def test_fetch_coingecko_icon_success() -> None:
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{_USDC_LOWER}"
    image_url = "https://assets.coingecko.com/coins/images/1/small/usdc.png"
    with respx.mock() as mock:
        mock.get(contract_url).mock(
            return_value=Response(200, json={"image": {"small": image_url}})
        )
        mock.get(image_url).mock(
            return_value=Response(200, content=_PNG_BYTES, headers={"content-type": "image/png"})
        )
        async with AsyncClient() as client:
            icon = await fetch_coingecko_icon(
                client, _USDC_LOWER, rate_limiter=_fast_rate_limiter()
            )

    assert icon is not None
    assert icon.source == "coingecko"
    assert icon.data == _PNG_BYTES


@pytest.mark.contract
async def test_fetch_coingecko_icon_404_returns_none() -> None:
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{_USDC_LOWER}"
    with respx.mock() as mock:
        mock.get(contract_url).mock(return_value=Response(404))
        async with AsyncClient() as client:
            icon = await fetch_coingecko_icon(
                client, _USDC_LOWER, rate_limiter=_fast_rate_limiter()
            )

    assert icon is None


@pytest.mark.contract
async def test_fetch_coingecko_icon_429_raises_rate_limited() -> None:
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{_USDC_LOWER}"
    with respx.mock() as mock:
        mock.get(contract_url).mock(return_value=Response(429))
        async with AsyncClient() as client:
            with pytest.raises(IconRateLimitedError):
                await fetch_coingecko_icon(client, _USDC_LOWER, rate_limiter=_fast_rate_limiter())


@pytest.mark.contract
async def test_fetch_coingecko_icon_server_error_raises_fetch_error() -> None:
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{_USDC_LOWER}"
    with respx.mock() as mock:
        mock.get(contract_url).mock(return_value=Response(500))
        async with AsyncClient() as client:
            with pytest.raises(IconFetchError):
                await fetch_coingecko_icon(client, _USDC_LOWER, rate_limiter=_fast_rate_limiter())
