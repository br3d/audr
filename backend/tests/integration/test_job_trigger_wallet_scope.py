"""POST /jobs wallet_id scoping (AUD-399/AUD-400).

The founder asked for "Refresh balances" / "Discover tokens" to move onto
each wallet card so a single address can be refreshed without burning RPC
calls on every tracked wallet. These tests lock in the API contract: a
scoped request persists its wallet_id on the job_run row, only coalesces
onto a run of the same kind AND the same scope, rejects wallet_id for kinds
that are not wallet-scoped, and 404s on an unknown wallet id.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

_BASE = "http://test"
_V1 = "/api/v1"
_PASSWORD = "correct-horse-battery-staple-99"
_WALLET_A = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
_WALLET_B = "0x00000000219ab540356cbb839cbe05303d7705fa"


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = ("job_run", "wallet", "login_attempt", "session", "owner")


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for tbl in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {tbl}"))  # noqa: S608 — tbl comes from the hardcoded _CLEAN_ORDER tuple above, not user input


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


@pytest.fixture()
async def auth_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[httpx.AsyncClient, str]]:
    """ASGI client with owner initialised; yields (client, csrf_token)."""
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as c:
            r = await c.post(f"{_V1}/setup", json={"password": _PASSWORD})
            assert r.status_code == 201, f"setup failed: {r.text}"
            csrf = r.json()["csrf_token"]
            yield c, csrf
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _add_wallet(
    client: httpx.AsyncClient, csrf: str, *, address: str, label: str = ""
) -> str:
    r = await client.post(
        f"{_V1}/wallets",
        json={"address": address, "label": label},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201, f"wallet creation failed: {r.text}"
    return r.json()["id"]


@pytest.mark.integration
async def test_trigger_job_with_wallet_id_persists_params(
    auth_client: tuple[httpx.AsyncClient, str],
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    c, csrf = auth_client
    wallet_id = await _add_wallet(c, csrf, address=_WALLET_A)

    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances", "wallet_id": wallet_id},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json()["coalesced"] is False

    async with db_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT params ->> 'wallet_id' FROM job_run WHERE id = :id"),
                {"id": run_id},
            )
        ).first()
    assert row is not None
    assert row[0] == wallet_id


@pytest.mark.integration
async def test_trigger_job_wallet_scope_does_not_cross_coalesce(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """A wallet-A request must not coalesce onto a wallet-B pending run."""
    c, csrf = auth_client
    wallet_a = await _add_wallet(c, csrf, address=_WALLET_A, label="A")
    wallet_b = await _add_wallet(c, csrf, address=_WALLET_B, label="B")

    first = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances", "wallet_id": wallet_a},
        headers={"x-csrf-token": csrf},
    )
    assert first.status_code == 202
    assert first.json()["coalesced"] is False
    first_run_id = first.json()["run_id"]

    second = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances", "wallet_id": wallet_b},
        headers={"x-csrf-token": csrf},
    )
    assert second.status_code == 202
    assert second.json()["coalesced"] is False
    assert second.json()["run_id"] != first_run_id, (
        "a wallet-B request must not coalesce onto wallet-A's pending run"
    )

    # A second wallet-A request, while A's is still pending, must coalesce.
    third = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances", "wallet_id": wallet_a},
        headers={"x-csrf-token": csrf},
    )
    assert third.status_code == 202
    assert third.json()["coalesced"] is True
    assert third.json()["run_id"] == first_run_id


@pytest.mark.integration
async def test_trigger_job_wallet_scope_does_not_cross_coalesce_with_global(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """A global (unscoped) run must not swallow, or be swallowed by, a scoped one."""
    c, csrf = auth_client
    wallet_a = await _add_wallet(c, csrf, address=_WALLET_A)

    global_run = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances"},
        headers={"x-csrf-token": csrf},
    )
    assert global_run.status_code == 202
    assert global_run.json()["coalesced"] is False

    scoped_run = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances", "wallet_id": wallet_a},
        headers={"x-csrf-token": csrf},
    )
    assert scoped_run.status_code == 202
    assert scoped_run.json()["coalesced"] is False
    assert scoped_run.json()["run_id"] != global_run.json()["run_id"]


@pytest.mark.integration
async def test_trigger_job_wallet_id_rejected_for_non_scoped_kind(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    c, csrf = auth_client
    wallet_id = await _add_wallet(c, csrf, address=_WALLET_A)

    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "quotes", "wallet_id": wallet_id},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.integration
async def test_trigger_job_unknown_wallet_id_404s(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances", "wallet_id": "00000000-0000-0000-0000-000000000000"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 404
