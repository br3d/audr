"""Integration tests for GET /assets, POST /assets/manual, PATCH /assets/{id} (AUD-317)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_ASSETS_URL = "/api/v1/assets"
_ASSETS_MANUAL_URL = "/api/v1/assets/manual"
_PASSWORD = "correct-horse-battery-staple-99"
_CONTRACT_A = "0x" + "a" * 40
_CONTRACT_B = "0x" + "b" * 40


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = (
    # valuation_* FK onto asset/wallet, so they must go first or the asset
    # delete below fails on rows left behind by earlier suites.
    "valuation_line",
    "valuation_snapshot",
    "monitored_pair",
    "balance_observation",
    "asset_metadata_revision",
    "asset",
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
    # outlive the module and pollute later suites.
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


async def _insert_asset(
    db_session_factory: async_sessionmaker[AsyncSession],
    token_address: str,
    symbol: str = "TKN",
    name: str = "Token",
    decimals: int = 18,
    source: str = "catalog",
    excluded: bool = False,
) -> str:
    asset_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)"
                    " VALUES (:id, :addr, :sym, :name, :dec, :src, :ex)"
                ),
                {
                    "id": asset_id,
                    "addr": token_address.lower(),
                    "sym": symbol,
                    "name": name,
                    "dec": decimals,
                    "src": source,
                    "ex": excluded,
                },
            )
    return asset_id


# ---------------------------------------------------------------------------
# GET /assets
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_assets_returns_200(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    await _insert_asset(db_session_factory, _CONTRACT_A, "WETH", "Wrapped Ether")
    r = await http_client.get(_ASSETS_URL)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "next_cursor" in data
    assert "request_id" in data
    assert "generated_at" in data
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["symbol"] == "WETH"
    assert item["chain_id"] == 1
    assert item["kind"] == "catalog"
    assert item["metadata_source"] == "catalog"
    assert item["excluded"] is False
    assert item["has_metadata_conflict"] is False


@pytest.mark.integration
async def test_list_assets_excluded_filter(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    await _insert_asset(db_session_factory, _CONTRACT_A, "TKN1", excluded=False)
    await _insert_asset(db_session_factory, _CONTRACT_B, "TKN2", excluded=True)

    r_all = await http_client.get(_ASSETS_URL)
    assert r_all.status_code == 200
    assert len(r_all.json()["items"]) == 2

    r_excl = await http_client.get(_ASSETS_URL, params={"excluded": "true"})
    assert r_excl.status_code == 200
    items = r_excl.json()["items"]
    assert len(items) == 1
    assert items[0]["excluded"] is True

    r_active = await http_client.get(_ASSETS_URL, params={"excluded": "false"})
    assert r_active.status_code == 200
    items = r_active.json()["items"]
    assert len(items) == 1
    assert items[0]["excluded"] is False


@pytest.mark.integration
async def test_list_assets_requires_session(http_client: httpx.AsyncClient) -> None:
    r = await http_client.get(_ASSETS_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /assets/manual
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_add_manual_asset_returns_201(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        _ASSETS_MANUAL_URL,
        json={"contract_address": _CONTRACT_A},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["contract_address"] == _CONTRACT_A
    assert data["kind"] == "manual"
    assert data["metadata_source"] == "owner"
    assert data["chain_id"] == 1
    assert data["excluded"] is False


@pytest.mark.integration
async def test_add_manual_asset_with_overrides(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        _ASSETS_MANUAL_URL,
        json={"contract_address": _CONTRACT_A, "decimals_override": 6, "symbol_override": "MYTKN"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["symbol"] == "MYTKN"
    assert data["decimals"] == 6


@pytest.mark.integration
async def test_add_manual_asset_normalises_address(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    mixed = "0x" + "A" * 40
    r = await http_client.post(
        _ASSETS_MANUAL_URL,
        json={"contract_address": mixed},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    assert r.json()["contract_address"] == mixed.lower()


@pytest.mark.integration
async def test_add_manual_asset_duplicate_returns_409(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    await _insert_asset(db_session_factory, _CONTRACT_A)
    r = await http_client.post(
        _ASSETS_MANUAL_URL,
        json={"contract_address": _CONTRACT_A},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 409
    body = r.json()
    # Contract error envelope (AUD-320): route diagnostics ride inside `error`.
    assert body["error"]["code"] == "asset_already_exists"
    assert "existing_id" in body["error"]


# ---------------------------------------------------------------------------
# PATCH /assets/{id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_patch_asset_excluded(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A, excluded=False)

    r = await http_client.patch(
        f"/api/v1/assets/{asset_id}",
        json={"excluded": True},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["excluded"] is True


@pytest.mark.integration
async def test_patch_asset_decimals_override(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A, decimals=18)

    r = await http_client.patch(
        f"/api/v1/assets/{asset_id}",
        json={"decimals_override": 6},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["decimals"] == 6


@pytest.mark.integration
async def test_patch_asset_not_found(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.patch(
        f"/api/v1/assets/{uuid.uuid4()}",
        json={"excluded": True},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 404


@pytest.mark.integration
async def test_patch_asset_requires_csrf(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A)
    r = await http_client.patch(
        f"/api/v1/assets/{asset_id}",
        json={"excluded": True},
    )
    assert r.status_code in (401, 403)
