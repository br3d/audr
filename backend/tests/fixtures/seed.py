"""Canonical seed fixtures for integration tests (AUD-286).

Provides the reference wallet (Buterin) and owner password used as the
standard test subject across the integration suite, plus a small set of
well-known ERC-20 assets for discovery / balance / valuation tests.

Constants
---------
BUTERIN_ADDRESS  – Vitalik Buterin's well-known Ethereum address (lowercase).
BUTERIN_LABEL    – Display name stored with the wallet.
TEST_PASSWORD    – Owner initialisation password.
MAINNET_RPC_URL  – Infura mainnet JSON-RPC endpoint used to seed the RPC integration.
WETH/USDC/USDT/DAI/LINK/UNI/AAVE constants – mainnet ERC-20 addresses.

Fixtures
--------
seeded_client    – AsyncClient with owner created; yields (client, csrf_token).
buterin_wallet   – Creates BUTERIN_ADDRESS wallet; returns wallet JSON dict.
seeded_assets    – Inserts the SEED_ASSETS list directly into the asset table.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUTERIN_ADDRESS: str = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
BUTERIN_LABEL: str = "Buterin"
TEST_PASSWORD: str = "Rand0mP@ssw0rd"
MAINNET_RPC_URL: str = "https://mainnet.infura.io/v3/4b1e7340470f489dbb76684df2861a9b"

# Well-known mainnet ERC-20 token addresses (all lowercase).
WETH_ADDRESS: str = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC_ADDRESS: str = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT_ADDRESS: str = "0xdac17f958d2ee523a2206206994597c13d831ec7"
DAI_ADDRESS: str = "0x6b175474e89094c44da98b954eedeac495271d0f"
LINK_ADDRESS: str = "0x514910771af9ca656af840dff83e8264ecf986ca"
UNI_ADDRESS: str = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"
AAVE_ADDRESS: str = "0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9"

# Seed asset records: (token_address, symbol, name, decimals)
SEED_ASSETS: list[tuple[str, str, str, int]] = [
    (WETH_ADDRESS, "WETH",  "Wrapped Ether",    18),
    (USDC_ADDRESS, "USDC",  "USD Coin",           6),
    (USDT_ADDRESS, "USDT",  "Tether USD",         6),
    (DAI_ADDRESS,  "DAI",   "Dai Stablecoin",    18),
    (LINK_ADDRESS, "LINK",  "ChainLink Token",   18),
    (UNI_ADDRESS,  "UNI",   "Uniswap",           18),
    (AAVE_ADDRESS, "AAVE",  "Aave Token",        18),
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_BASE_URL = "http://test"
_SETUP_URL = "/api/v1/setup"
_WALLETS_URL = "/api/v1/wallets"


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def seeded_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[httpx.AsyncClient, str]]:
    """ASGI client with an owner account initialised using TEST_PASSWORD.

    Yields:
        (client, csrf_token) — the client has a live session cookie set.

    Requires the caller's test function (or an autouse fixture in the same
    file) to have truncated the owner / session / login_attempt tables first,
    so that POST /setup succeeds with 201.
    """
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE_URL) as client:
            r = await client.post(_SETUP_URL, json={"password": TEST_PASSWORD})
            assert r.status_code == 201, f"Owner setup failed ({r.status_code}): {r.text}"
            csrf = r.json()["csrf_token"]
            yield client, csrf
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
async def buterin_wallet(
    seeded_client: tuple[httpx.AsyncClient, str],
) -> dict[str, Any]:
    """Creates the Buterin wallet and returns the API response dict.

    Depends on seeded_client, so the owner account is always present first.
    The wallet is created with BUTERIN_ADDRESS and BUTERIN_LABEL.
    """
    client, csrf = seeded_client
    r = await client.post(
        _WALLETS_URL,
        json={"address": BUTERIN_ADDRESS, "label": BUTERIN_LABEL},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201, f"Wallet creation failed ({r.status_code}): {r.text}"
    return r.json()


@pytest.fixture()
async def seeded_assets(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> list[dict[str, Any]]:
    """Inserts SEED_ASSETS into the asset table and returns the inserted rows.

    Each row is a dict with keys: id, token_address, symbol, name, decimals.

    Uses a committed transaction so the HTTP layer and other DB sessions
    can see the rows without relying on shared transaction state.
    """
    rows: list[dict[str, Any]] = []
    async with db_session_factory() as session:
        async with session.begin():
            for addr, symbol, name, decimals in SEED_ASSETS:
                row_id = uuid.uuid4()
                await session.execute(
                    text(
                        "INSERT INTO asset (id, token_address, symbol, name, decimals,"
                        " source, excluded)"
                        " VALUES (:id, :addr, :sym, :name, :dec, 'seed', false)"
                        " ON CONFLICT (token_address) DO NOTHING"
                    ),
                    {
                        "id": str(row_id),
                        "addr": addr,
                        "sym": symbol,
                        "name": name,
                        "dec": decimals,
                    },
                )
                rows.append(
                    {
                        "id": str(row_id),
                        "token_address": addr,
                        "symbol": symbol,
                        "name": name,
                        "decimals": decimals,
                    }
                )
    return rows
