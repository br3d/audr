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
                    f"INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at, input_key)"
                    f" VALUES (:id, NOW(), :quality, {published_clause}, :input_key)"
                ),
                {"id": snap_id, "quality": quality, "input_key": snap_id},
            )

            for h in holdings or []:
                wallet_id = h["wallet_id"]
                asset_id = h["asset_id"]
                raw_amount = h.get("raw_amount", "1000000000000000000")
                block_number = h.get("block_number", 12345678)
                block_time = h.get("block_time")
                price_usd = h.get("price_usd")
                value_usd = h.get("value_usd")

                await session.execute(
                    text(
                        "INSERT INTO valuation_line"
                        " (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number, block_time, price_usd, value_usd)"
                        " VALUES (:id, :snap, :wallet, :asset, :raw, :block, :block_time, :price, :value)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "snap": snap_id,
                        "wallet": wallet_id,
                        "asset": asset_id,
                        "raw": raw_amount,
                        "block": block_number,
                        "block_time": block_time,
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
async def test_portfolio_gaps_quality_yields_total_from_priced_holdings(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """quality='gaps' (AUD-361): total_usd sums what's priced, unavailable asset is counted.

    This is the AUD-361 regression: a portfolio with a handful of dust/exotic
    holdings the price provider will never resolve must still report a real
    total_usd, not a permanent null — but the excluded asset is still surfaced
    via unpriced_asset_count rather than silently dropped.
    """
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    priced_asset_id = await _seed_asset(db_session_factory, token_address="0x" + "a" * 40)
    dust_asset_id = await _seed_asset(db_session_factory, token_address="0x" + "b" * 40, symbol="DUST")
    await _seed_snapshot(
        db_session_factory,
        quality="gaps",
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": priced_asset_id,
                "price_usd": "100.0",
                "value_usd": "100.0",
            },
            {
                "wallet_id": wallet_id,
                "asset_id": dust_asset_id,
                "price_usd": None,
                "value_usd": None,
            },
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["total_usd"] == "100.000000000000000000"
    assert data["unpriced_asset_count"] == 1
    quality = data["quality"]
    assert quality["incomplete"] is False
    assert quality["stale_prices"] is False


@pytest.mark.integration
async def test_portfolio_stale_quality_sets_stale_prices_flag(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """quality='stale' (zero holdings priced at all) is the only state reporting stale_prices.

    'partial'/'gaps' are coverage gaps, not price staleness (AUD-361) — only
    a snapshot where nothing at all got priced should set this flag.
    """
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory)
    await _seed_snapshot(
        db_session_factory,
        quality="stale",
        holdings=[
            {"wallet_id": wallet_id, "asset_id": asset_id, "price_usd": None, "value_usd": None}
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    quality = r.json()["quality"]
    assert quality["stale_prices"] is True


@pytest.mark.integration
async def test_portfolio_exposes_block_time_and_stale_contribution(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AUD-72: block_time / stale_contribution_usd are computed, not null stubs.

    Two holdings land in the same snapshot from different balance_scan runs
    (different block_number/block_time pairs): the older one is "carried
    forward" and its value contributes to stale_contribution_usd, while
    balance_block_time tracks the newer, freshest block. The chain's block
    time must be distinguishable from the server's own valuation_time (the
    quote-refresh clock) — they are different clocks and must not collapse
    to the same value.
    """
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    fresh_asset_id = await _seed_asset(db_session_factory, token_address="0x" + "a" * 40, symbol="FRESH")
    stale_asset_id = await _seed_asset(db_session_factory, token_address="0x" + "b" * 40, symbol="STALE")

    older_block_time = "2026-01-01T00:00:00+00:00"
    newer_block_time = "2026-01-02T00:00:00+00:00"

    snap_id = await _seed_snapshot(
        db_session_factory,
        quality="complete",
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": fresh_asset_id,
                "block_number": 20000100,
                "block_time": newer_block_time,
                "price_usd": "100.0",
                "value_usd": "100.0",
            },
            {
                "wallet_id": wallet_id,
                "asset_id": stale_asset_id,
                "block_number": 20000000,
                "block_time": older_block_time,
                "price_usd": "50.0",
                "value_usd": "50.0",
            },
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["snapshot_id"] == snap_id

    # Freshest block among the holdings wins at the envelope level.
    assert data["balance_block"] == 20000100
    assert data["balance_block_time"] == newer_block_time

    # Only the older holding's value counts as carried-forward.
    assert data["stale_contribution_usd"] == "50.000000000000000000"

    holdings_by_asset = {h["asset_id"]: h for h in data["holdings"]}
    assert holdings_by_asset[fresh_asset_id]["block_time"] == newer_block_time
    assert holdings_by_asset[stale_asset_id]["block_time"] == older_block_time

    # The chain's block time and the server's quote/valuation clock are
    # independent — this must not be a copy of valuation_time.
    assert data["balance_block_time"] != data["valuation_time"]


@pytest.mark.integration
async def test_portfolio_fully_fresh_snapshot_has_zero_stale_contribution(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When every holding shares the latest block, nothing is carried forward."""
    await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory)
    await _seed_snapshot(
        db_session_factory,
        quality="complete",
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": asset_id,
                "block_number": 20000000,
                "block_time": "2026-01-01T00:00:00+00:00",
                "price_usd": "10.0",
                "value_usd": "10.0",
            }
        ],
    )

    r = await http_client.get(_PORTFOLIO_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["stale_contribution_usd"] == "0.000000000000000000"
    assert data["balance_block_time"] == "2026-01-01T00:00:00+00:00"


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
