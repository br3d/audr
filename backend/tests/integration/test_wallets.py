"""Integration tests for wallet management routes (T032 / T033 / US1).

Requires a live PostgreSQL test database with migrations applied through 003.

Covers:
  - POST /api/v1/wallets: add wallet, duplicate, invalid address
  - GET /api/v1/wallets: list wallets
  - GET /api/v1/wallets/{id}: get single wallet
  - PATCH /api/v1/wallets/{id}: update label
  - POST /api/v1/wallets/{id}/stop: stop scanning
  - POST /api/v1/wallets/{id}/reactivate: resume scanning
  - Address normalisation: mixed-case input stored as lowercase
  - CSRF enforcement on mutating endpoints
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_WALLETS_URL = "/api/v1/wallets"
_PASSWORD = "correct-horse-battery-staple-99"
_ADDR_A = "0x" + "a" * 40
_ADDR_B = "0x" + "b" * 40
# Token address owned by this module only — committed by the delete tests, so it
# must not collide with the fixtures of any other test module.
_TOKEN_ADDR = "0x3670000000000000000000000000000000000367"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_tables(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Truncate relevant tables before every test."""
    async with db_session_factory() as session:
        async with session.begin():
            # Wallet-referencing rows first — the delete tests seed some, and a
            # failed assertion there would otherwise wedge every later test on
            # the wallet foreign keys.
            await session.execute(text("DELETE FROM balance_observation"))
            await session.execute(text("DELETE FROM monitored_pair"))
            await session.execute(text("DELETE FROM wallet"))
            await session.execute(
                text("DELETE FROM asset WHERE token_address = :addr"),
                {"addr": _TOKEN_ADDR},
            )
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


async def _add_wallet(
    client: httpx.AsyncClient,
    csrf: str,
    address: str = _ADDR_A,
    label: str = "My Wallet",
) -> httpx.Response:
    return await client.post(
        _WALLETS_URL,
        json={"address": address, "label": label},
        headers={"x-csrf-token": csrf},
    )


# ---------------------------------------------------------------------------
# POST /wallets tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_add_wallet_returns_201(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await _add_wallet(http_client, csrf)
    assert r.status_code == 201
    data = r.json()
    assert data["address"] == _ADDR_A
    assert data["label"] == "My Wallet"
    assert data["tracking_active"] is True
    assert "id" in data


@pytest.mark.integration
async def test_add_wallet_normalises_address_to_lowercase(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    mixed = "0x" + "A" * 40
    r = await _add_wallet(http_client, csrf, address=mixed)
    assert r.status_code == 201
    assert r.json()["address"] == mixed.lower()


@pytest.mark.integration
async def test_add_duplicate_wallet_returns_409(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    await _add_wallet(http_client, csrf)
    r = await _add_wallet(http_client, csrf)
    assert r.status_code == 409


@pytest.mark.integration
async def test_add_invalid_address_returns_422(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await _add_wallet(http_client, csrf, address="not-an-address")
    assert r.status_code == 422


@pytest.mark.integration
async def test_add_wallet_without_csrf_returns_403(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.post(_WALLETS_URL, json={"address": _ADDR_A, "label": ""})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# GET /wallets tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_list_wallets_empty(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.get(_WALLETS_URL, headers={"x-csrf-token": csrf})
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.integration
async def test_list_wallets_returns_added_wallets(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    await _add_wallet(http_client, csrf, address=_ADDR_A, label="A")
    await _add_wallet(http_client, csrf, address=_ADDR_B, label="B")
    r = await http_client.get(_WALLETS_URL, headers={"x-csrf-token": csrf})
    assert r.status_code == 200
    addresses = [w["address"] for w in r.json()["items"]]
    assert _ADDR_A in addresses
    assert _ADDR_B in addresses


@pytest.mark.integration
async def test_list_wallets_paginates_with_cursor(http_client: httpx.AsyncClient) -> None:
    """AUD-322: cursor is honoured and next_cursor advances until exhausted."""
    csrf = await _setup_and_get_csrf(http_client)
    addr_c = "0x" + "c" * 40
    await _add_wallet(http_client, csrf, address=_ADDR_A, label="A")
    await _add_wallet(http_client, csrf, address=_ADDR_B, label="B")
    await _add_wallet(http_client, csrf, address=addr_c, label="C")

    r1 = await http_client.get(
        _WALLETS_URL, params={"limit": 2}, headers={"x-csrf-token": csrf}
    )
    assert r1.status_code == 200
    page1 = r1.json()
    assert [w["address"] for w in page1["items"]] == [_ADDR_A, _ADDR_B]
    assert page1["next_cursor"] is not None

    r2 = await http_client.get(
        _WALLETS_URL,
        params={"limit": 2, "cursor": page1["next_cursor"]},
        headers={"x-csrf-token": csrf},
    )
    assert r2.status_code == 200
    page2 = r2.json()
    assert [w["address"] for w in page2["items"]] == [addr_c]
    assert page2["next_cursor"] is None


@pytest.mark.integration
async def test_list_wallets_invalid_cursor_returns_400(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.get(
        _WALLETS_URL,
        params={"cursor": "not-a-uuid"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /wallets/{id} tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_wallet_by_id(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    created = (await _add_wallet(http_client, csrf)).json()
    wallet_id = created["id"]

    r = await http_client.get(
        f"{_WALLETS_URL}/{wallet_id}", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert r.json()["id"] == wallet_id


@pytest.mark.integration
async def test_get_wallet_unknown_id_returns_404(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.get(
        f"{_WALLETS_URL}/{uuid.uuid4()}", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /wallets/{id} tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_patch_wallet_label(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = (await _add_wallet(http_client, csrf)).json()["id"]

    r = await http_client.patch(
        f"{_WALLETS_URL}/{wallet_id}",
        json={"label": "Renamed"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    assert r.json()["label"] == "Renamed"


# ---------------------------------------------------------------------------
# POST /wallets/{id}/stop and /reactivate tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_stop_wallet(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = (await _add_wallet(http_client, csrf)).json()["id"]

    r = await http_client.post(
        f"{_WALLETS_URL}/{wallet_id}/stop", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert r.json()["tracking_active"] is False


@pytest.mark.integration
async def test_reactivate_wallet(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = (await _add_wallet(http_client, csrf)).json()["id"]

    await http_client.post(
        f"{_WALLETS_URL}/{wallet_id}/stop", headers={"x-csrf-token": csrf}
    )
    r = await http_client.post(
        f"{_WALLETS_URL}/{wallet_id}/reactivate", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert r.json()["tracking_active"] is True


@pytest.mark.integration
async def test_stop_unknown_wallet_returns_404(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        f"{_WALLETS_URL}/{uuid.uuid4()}/stop", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /wallets/{id} tests (AUD-367)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_delete_wallet_removes_it_from_the_list(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = (await _add_wallet(http_client, csrf)).json()["id"]
    keep_id = (
        await _add_wallet(http_client, csrf, address=_ADDR_B, label="Keep")
    ).json()["id"]

    r = await http_client.delete(
        f"{_WALLETS_URL}/{wallet_id}", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["wallet_id"] == wallet_id
    assert body["deleted"]["wallet"] == 1

    listed = await http_client.get(_WALLETS_URL)
    assert [w["id"] for w in listed.json()["items"]] == [keep_id]
    assert (await http_client.get(f"{_WALLETS_URL}/{wallet_id}")).status_code == 404


@pytest.mark.integration
async def test_delete_wallet_also_removes_its_derived_records(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deleting an address must not leave orphaned observations behind — the
    whole point of delete over stop is that nothing of it is left."""
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = (await _add_wallet(http_client, csrf)).json()["id"]

    async with db_session_factory() as session:
        async with session.begin():
            asset_id = (
                await session.execute(
                    text(
                        "INSERT INTO asset (token_address, symbol, name, decimals,"
                        " source)"
                        " VALUES (:addr, 'TKN', 'Token', 18, 'manual')"
                        " RETURNING id"
                    ),
                    {"addr": _TOKEN_ADDR},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO balance_observation (wallet_id, asset_id,"
                    " raw_amount, block_number)"
                    " VALUES (:wid, :aid, 1, 1)"
                ),
                {"wid": wallet_id, "aid": asset_id},
            )
            await session.execute(
                text(
                    "INSERT INTO monitored_pair (wallet_id, asset_id)"
                    " VALUES (:wid, :aid)"
                ),
                {"wid": wallet_id, "aid": asset_id},
            )

    r = await http_client.delete(
        f"{_WALLETS_URL}/{wallet_id}", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 200
    assert r.json()["deleted"]["balance_observation"] == 1
    assert r.json()["deleted"]["monitored_pair"] == 1

    async with db_session_factory() as session:
        for table in ("balance_observation", "monitored_pair"):
            left = await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE wallet_id = :wid"),
                {"wid": wallet_id},
            )
            assert left.scalar() == 0, table


@pytest.mark.integration
async def test_delete_unknown_wallet_returns_404(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.delete(
        f"{_WALLETS_URL}/{uuid.uuid4()}", headers={"x-csrf-token": csrf}
    )
    assert r.status_code == 404


@pytest.mark.integration
async def test_delete_wallet_requires_csrf(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = (await _add_wallet(http_client, csrf)).json()["id"]

    r = await http_client.delete(f"{_WALLETS_URL}/{wallet_id}")
    assert r.status_code == 403
    assert (await http_client.get(f"{_WALLETS_URL}/{wallet_id}")).status_code == 200
