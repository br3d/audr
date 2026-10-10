"""Integration tests for GET /assets, POST /assets/manual, PATCH /assets/{id} (AUD-317, AUD-433)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

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
_CONTRACT_C = "0x" + "c" * 40


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
    "discovery_coverage",
    "balance_observation",
    "asset_metadata_revision",
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
                await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 -- table is from the fixed _CLEAN_ORDER tuple


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
                    "INSERT INTO asset"
                    " (id, token_address, symbol, name, decimals, source, excluded)"
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


async def _insert_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
    address: str,
) -> str:
    wallet_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO wallet (id, address, label_ciphertext) VALUES (:id, :addr, '')"),
                {"id": wallet_id, "addr": address.lower()},
            )
    return wallet_id


async def _insert_balance_observation(
    db_session_factory: async_sessionmaker[AsyncSession],
    wallet_id: str,
    asset_id: str,
    raw_amount: int,
    block_number: int = 1,
    observed_at: datetime | None = None,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO balance_observation"
                    " (wallet_id, asset_id, raw_amount, block_number, observed_at)"
                    " VALUES (:wallet_id, :asset_id, :raw_amount, :block_number,"
                    " COALESCE(:observed_at, now()))"
                ),
                {
                    "wallet_id": wallet_id,
                    "asset_id": asset_id,
                    "raw_amount": raw_amount,
                    "block_number": block_number,
                    "observed_at": observed_at,
                },
            )


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
# GET /assets?held=... (AUD-433)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_assets_held_defaults_to_full_catalog(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No `held` param -> unchanged behaviour: the whole catalog, `held` flag included."""
    await _setup_and_get_csrf(http_client)
    wallet_id = await _insert_wallet(db_session_factory, "0x" + "1" * 40)
    held_asset = await _insert_asset(db_session_factory, _CONTRACT_A, "HELD")
    await _insert_asset(db_session_factory, _CONTRACT_B, "CANDIDATE")
    await _insert_balance_observation(db_session_factory, wallet_id, held_asset, raw_amount=100)

    r = await http_client.get(_ASSETS_URL)
    assert r.status_code == 200
    items = {item["symbol"]: item for item in r.json()["items"]}
    assert len(items) == 2
    assert items["HELD"]["held"] is True
    assert items["CANDIDATE"]["held"] is False


@pytest.mark.integration
async def test_list_assets_held_true_filters_to_nonzero_latest_balance(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_id = await _insert_wallet(db_session_factory, "0x" + "1" * 40)
    held_asset = await _insert_asset(db_session_factory, _CONTRACT_A, "HELD")
    candidate_asset = await _insert_asset(db_session_factory, _CONTRACT_B, "CANDIDATE")
    await _insert_balance_observation(db_session_factory, wallet_id, held_asset, raw_amount=100)

    r = await http_client.get(_ASSETS_URL, params={"held": "true"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "HELD"
    assert items[0]["held"] is True
    assert candidate_asset  # kept out of the held=true result


@pytest.mark.integration
async def test_list_assets_held_false_returns_unheld_only(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_id = await _insert_wallet(db_session_factory, "0x" + "1" * 40)
    held_asset = await _insert_asset(db_session_factory, _CONTRACT_A, "HELD")
    await _insert_asset(db_session_factory, _CONTRACT_B, "CANDIDATE")
    await _insert_balance_observation(db_session_factory, wallet_id, held_asset, raw_amount=100)

    r = await http_client.get(_ASSETS_URL, params={"held": "false"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "CANDIDATE"
    assert items[0]["held"] is False
    assert held_asset  # kept out of the held=false result


@pytest.mark.integration
async def test_list_assets_held_uses_latest_observation_not_ever_held(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """held must track the LATEST (wallet, asset) observation, not "ever nonzero"."""
    await _setup_and_get_csrf(http_client)
    wallet_id = await _insert_wallet(db_session_factory, "0x" + "1" * 40)
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A, "DRAINED")
    base = datetime(2025, 6, 1, tzinfo=UTC)
    await _insert_balance_observation(
        db_session_factory, wallet_id, asset_id, raw_amount=100, observed_at=base
    )
    await _insert_balance_observation(
        db_session_factory,
        wallet_id,
        asset_id,
        raw_amount=0,
        block_number=2,
        observed_at=base + timedelta(hours=1),
    )

    r_true = await http_client.get(_ASSETS_URL, params={"held": "true"})
    assert r_true.json()["items"] == []

    r_false = await http_client.get(_ASSETS_URL, params={"held": "false"})
    items = r_false.json()["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "DRAINED"
    assert items[0]["held"] is False


@pytest.mark.integration
async def test_list_assets_held_true_includes_excluded_asset(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An excluded-but-held asset must stay reachable via held=true&excluded=true.

    Regression guard: held must NOT be implemented as "held AND NOT excluded" —
    the UI needs a way to un-exclude an asset it is still holding.
    """
    await _setup_and_get_csrf(http_client)
    wallet_id = await _insert_wallet(db_session_factory, "0x" + "1" * 40)
    excluded_held_asset = await _insert_asset(
        db_session_factory, _CONTRACT_A, "EXCLUDED_HELD", excluded=True
    )
    await _insert_balance_observation(
        db_session_factory, wallet_id, excluded_held_asset, raw_amount=100
    )

    r = await http_client.get(_ASSETS_URL, params={"held": "true", "excluded": "true"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["symbol"] == "EXCLUDED_HELD"
    assert items[0]["held"] is True
    assert items[0]["excluded"] is True


@pytest.mark.integration
async def test_list_assets_held_paginates_with_cursor(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    wallet_id = await _insert_wallet(db_session_factory, "0x" + "1" * 40)
    asset_ids = []
    for i, contract in enumerate((_CONTRACT_A, _CONTRACT_B, _CONTRACT_C)):
        asset_id = await _insert_asset(db_session_factory, contract, f"HELD{i}")
        await _insert_balance_observation(db_session_factory, wallet_id, asset_id, raw_amount=1)
        asset_ids.append(asset_id)

    seen: list[str] = []
    cursor = None
    for _ in range(3):
        params = {"held": "true", "limit": "2"}
        if cursor:
            params["cursor"] = cursor
        r = await http_client.get(_ASSETS_URL, params=params)
        assert r.status_code == 200
        body = r.json()
        seen.extend(item["symbol"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert sorted(seen) == ["HELD0", "HELD1", "HELD2"]


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
