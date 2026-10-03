"""Keyless asset icon resolution (rotki-style, AUD-385).

Resolves a token's logo without any API key, in priority order:

1. Trust Wallet's GitHub asset repo, addressed by EIP-55 checksummed
   contract address (native ETH uses the chain's `info/logo.png`).
2. CoinGecko's keyless public contract-lookup endpoint
   (`/coins/ethereum/contract/{address}`), which still enforces a tight,
   unpublished per-IP rate limit even without a key. Callers must pass the
   shared `RateLimiter` from `audr.jobs.policy.get_shared_asset_icon_rate_limiter`
   and must not treat a 429 (`IconRateLimitedError`) the same as "no icon" —
   a rate-limited lookup belongs on the next refresh run, never negative-cached.

Every downloaded image is read as a bounded stream (`_MAX_ICON_BYTES`) and
checked against `_ALLOWED_CONTENT_TYPES` before being handed back — this is
the only place the backend ingests bytes from the open internet without a
schema, so it is deliberately strict about what it will store.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx
from eth_utils import to_checksum_address

from audr.assets.constants import is_native_eth
from audr.jobs.policy import RateLimiter

logger = logging.getLogger(__name__)

_TRUST_WALLET_BASE = (
    "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ethereum"
)
_COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_TIMEOUT = 5.0
_MAX_ICON_BYTES = 256 * 1024
_ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}


class IconFetchError(Exception):
    """Raised for an unexpected (non-404) failure while fetching an icon."""


class IconRateLimitedError(IconFetchError):
    """Raised when a keyless provider rate-limits the request (AUD-385).

    Must never be interpreted as "no icon" — the caller leaves no asset_icon
    row behind so the next refresh run retries instead of negative-caching an
    icon that may well exist.
    """


@dataclass(frozen=True)
class IconImage:
    content_type: str
    data: bytes
    source: str


def trust_wallet_logo_url(token_address: str) -> str:
    """Return the Trust Wallet raw.githubusercontent.com logo URL for *token_address*."""
    if is_native_eth(token_address):
        return f"{_TRUST_WALLET_BASE}/info/logo.png"
    checksummed = to_checksum_address(token_address)
    return f"{_TRUST_WALLET_BASE}/assets/{checksummed}/logo.png"


async def fetch_trust_wallet_icon(
    client: httpx.AsyncClient, token_address: str
) -> IconImage | None:
    """Return the Trust Wallet icon for *token_address*, or None if it has none."""
    url = trust_wallet_logo_url(token_address)
    try:
        downloaded = await _download_image(client, url)
    except IconFetchError as exc:
        logger.info("asset_icons: trustwallet fetch failed for %s: %s", token_address, exc)
        return None
    if downloaded is None:
        return None
    data, content_type = downloaded
    return IconImage(content_type=content_type, data=data, source="trustwallet")


async def fetch_coingecko_icon(
    client: httpx.AsyncClient, token_address: str, *, rate_limiter: RateLimiter
) -> IconImage | None:
    """Return the CoinGecko keyless contract-lookup icon for *token_address*.

    Raises IconRateLimitedError on 429 — callers must treat that as "try
    again later", never as "no icon" (AUD-385).
    """
    await rate_limiter.acquire()
    url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{token_address.lower()}"
    response = await client.get(url, timeout=_TIMEOUT)
    if response.status_code == 404:
        return None
    if response.status_code == 429:
        raise IconRateLimitedError(url)
    if not response.is_success:
        raise IconFetchError(f"GET {url} -> HTTP {response.status_code}")

    data = json.loads(response.text, parse_float=Decimal)
    image_url = (data.get("image") or {}).get("small")
    if not image_url:
        return None

    try:
        downloaded = await _download_image(client, image_url)
    except IconFetchError as exc:
        logger.info("asset_icons: coingecko image fetch failed for %s: %s", token_address, exc)
        return None
    if downloaded is None:
        return None
    image_data, content_type = downloaded
    return IconImage(content_type=content_type, data=image_data, source="coingecko")


async def _download_image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    """Stream *url*, enforcing the timeout/size/content-type allowlist.

    Returns None for a 404 (no icon there); raises IconFetchError for any
    other non-2xx status, a disallowed content-type, or a response exceeding
    `_MAX_ICON_BYTES` — read as a bounded stream so an oversized response
    never fully lands in memory before being rejected.
    """
    async with client.stream("GET", url, timeout=_TIMEOUT) as response:
        if response.status_code == 404:
            return None
        if response.status_code == 429:
            raise IconRateLimitedError(url)
        if not response.is_success:
            raise IconFetchError(f"GET {url} -> HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise IconFetchError(f"GET {url} -> disallowed content-type {content_type!r}")

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > _MAX_ICON_BYTES:
                raise IconFetchError(f"GET {url} -> response exceeded {_MAX_ICON_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks), content_type
