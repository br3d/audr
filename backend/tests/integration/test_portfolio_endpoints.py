"""Integration tests for GET /portfolio (AUD-317)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_PORTFOLIO_URL = "/api/v1/portfolio"
_PASSWORD = "correct-horse-battery-staple-99"


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = (
    "valuation_line",
    "valuation_snapshot",
    "balance_observation",
    "monitored_pair",
    "asset",
    "wallet",
    "login_attempt",
    "session",
    "owner",
)


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for table in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {table}"))


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    # Wipe before *and* after: these routes commit, so rows left behind would
    # outlive the module and break later suites that DELETE FROM wallet.
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


@pytest.fixture()
async def http_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[httpx.AsyncClient]:
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


async def _seed_snapshot(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    quality: str = "complete",
    published: bool = True,
    holdings: list[dict] | None = None,
) -> str:
    """Insert a valuation_snapshot (and optional lines) and return the snapshot id."""
    snap_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            published_clause = "NOW()" if published else "NULL"
            await session.execute(
                text(
                    f"INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at)"
                    f" VALUES (:id, NOW(), :quality, {published_clause})"
                ),
                {"id": snap_id, "quality": quality},
            )

            for h in holdings or []:
                wallet_id = h["wallet_id"]
                asset_id = h["asset_id"]
                raw_amount = h.get("raw_amount", "1000000000000000000")
                block_number = h.get("block_number", 12345678)
                price_usd = h.get("price_usd")
                value_usd = h.get("value_usd")

                await session.execute(
                    text(
                        "INSERT INTO valuation_line"
                        " (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number, price_usd, value_usd)"
                        " VALUES (:id, :snap, :wallet, :asset, :raw, :block, :price, :value)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "snap": snap_id,
                        "wallet": wallet_id,
                        "asset": asset_id,
                        "raw": raw_amount,
                        "block": block_number,
                        "price": price_usd,
                        "value": value_usd,
                    },
                )
    return snap_id


async def _seed_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
    address: str = "0x" + "a" * 40,
) -> str:
    wallet_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO wallet (id, address, label, status) VALUES (:id, :addr, '', 'active')"),
                {"id": wallet_id, "addr": address.lower()},
            )
    return wallet_id


async def _seed_asset(
    db_session_factory: async_sessionmaker[AsyncSession],
    token_address: str = "0x" + "b" * 40,
    symbol: str = "TKN",
    decimals: int = 18,
    source: str = "catalog",
) -> str:
    asset_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                    " VALUES (:id, :addr, :sym, :sym, :dec, :src)"
                ),
                {"id": asset_id, "addr": token_address.lower(), "sym": symbol, "dec": decimals, "src": source},
            )
    return asset_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_portfolio_requires_session(http_client: httpx.AsyncClient) -> None:
    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 401


@pytest.mark.integration
async def test_portfolio_empty_when_no_snapshot(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["snapshot_id"] is None
    assert data["total_usd"] is None
    assert data["priced_subtotal_usd"] is None
    assert data["holdings"] == []
    assert data["allocations"] == []
    assert data["currency"] == "USD"
    assert "request_id" in data
    assert "generated_at" in data
    assert "quality" in data


@pytest.mark.integration
async def test_portfolio_unpublished_snapshot_returns_empty(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    await _seed_snapshot(db_session_factory, published=False)
    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    assert r.json()["snapshot_id"] is None


@pytest.mark.integration
async def test_portfolio_returns_snapshot_with_holdings(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory, symbol="WETH", decimals=18)

    raw = "2000000000000000000"  # 2 ETH
    price = "3000.000000000000000000"
    value = "6000.000000000000000000"
    snap_id = await _seed_snapshot(
        db_session_factory,
        quality="complete",
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": asset_id,
                "raw_amount": raw,
                "block_number": 20000000,
                "price_usd": price,
                "value_usd": value,
            }
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["snapshot_id"] == snap_id
    assert data["currency"] == "USD"
    assert data["balance_block"] == 20000000
    assert len(data["holdings"]) == 1

    holding = data["holdings"][0]
    assert holding["wallet_id"] == wallet_id
    assert holding["asset_id"] == asset_id
    assert holding["raw_balance"] == raw
    assert holding["decimals"] == 18
    assert holding["price_usd"] is not None
    assert holding["value_usd"] is not None
    assert holding["included"] is True
    assert holding["read_status"] == "ok"

    # Allocations computed.
    assert len(data["allocations"]) == 1
    alloc = data["allocations"][0]
    assert alloc["asset_id"] == asset_id
    assert alloc["symbol"] == "WETH"
    assert alloc["percentage"] == "100.00"


@pytest.mark.integration
async def test_portfolio_partial_quality_sets_incomplete_flag(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory)
    await _seed_snapshot(
        db_session_factory,
        quality="partial",
        holdings=[
            {"wallet_id": wallet_id, "asset_id": asset_id, "price_usd": None, "value_usd": None}
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    quality = r.json()["quality"]
    assert quality["incomplete"] is True


@pytest.mark.integration
async def test_portfolio_total_usd_null_when_missing_price(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory)
    await _seed_snapshot(
        db_session_factory,
        quality="partial",
        holdings=[
            {"wallet_id": wallet_id, "asset_id": asset_id, "price_usd": None, "value_usd": None}
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    data = r.json()
    # total_usd must be null when any included holding has no price (unknown ≠ zero).
    assert data["total_usd"] is None


@pytest.mark.integration
async def test_portfolio_wallet_id_filter(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_a = await _seed_wallet(db_session_factory, "0x" + "a" * 40)
    wallet_b = await _seed_wallet(db_session_factory, "0x" + "b" * 40)
    asset_id = await _seed_asset(db_session_factory)

    await _seed_snapshot(
        db_session_factory,
        quality="complete",
        holdings=[
            {"wallet_id": wallet_a, "asset_id": asset_id, "price_usd": "1.0", "value_usd": "1.0"},
            {"wallet_id": wallet_b, "asset_id": asset_id, "price_usd": "2.0", "value_usd": "2.0"},
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL, params={"wallet_id": wallet_a})
    assert r.status_code == 200
    holdings = r.json()["holdings"]
    assert all(h["wallet_id"] == wallet_a for h in holdings)
    assert len(holdings) == 1
