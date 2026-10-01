"""Integration tests for PATCH /api/v1/settings schedule validation (AUD-272).

Covers:
  - interval_seconds <= 0 is rejected with 422 (previously silently accepted)
  - freshness_seconds <= 0 is rejected with 422
  - a positive interval_seconds/freshness_seconds patch still applies and bumps revision
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
from audr.operations.init_key import init_key

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_SETTINGS_URL = "/api/v1/settings"
_PASSWORD = "correct-horse-battery-staple-99"

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> AsyncGenerator[None]:
    """Reset the committed state this module writes, before AND after each test.

    These tests use ``db_session_factory`` (real COMMITs), not the rolled-back
    ``db_session`` fixture. Cleaning only on setup left the final test's
    ``schedule`` rows in the database, so a second suite run against the same
    database failed in ``test_schedules.py`` with
    ``duplicate key value violates unique constraint "uq_schedule_kind"``.
    Tearing down as well keeps the suite re-runnable without
    ``docker compose down -v``.
    """
    await _delete_committed_state(db_session_factory)
    async with db_session_factory() as session:
        async with session.begin():
            await init_key(session)
    yield
    await _delete_committed_state(db_session_factory)


async def _delete_committed_state(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM schedule"))
            await session.execute(text("DELETE FROM key_state"))
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


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


async def _insert_schedule(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str,
    revision: int = 1,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO schedule (id, kind, enabled, revision)
                    VALUES (:id, :kind, true, :revision)
                    """
                ),
                {"id": str(uuid.uuid4()), "kind": kind, "revision": revision},
            )


async def test_get_settings_computes_revision_with_schedule_rows_present(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: _query_settings_response previously read paused_at (row[4])
    instead of revision (row[3]), crashing GET /settings with a TypeError as
    soon as any schedule row existed with paused_at NULL (the common case).
    """
    await _setup_and_get_csrf(http_client)
    await _insert_schedule(db_session_factory, kind="balance_scan", revision=3)

    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 200
    assert r.json()["revision"] == "3"


@pytest.mark.parametrize("field,value", [("interval_seconds", 0), ("interval_seconds", -30)])
async def test_patch_settings_rejects_non_positive_interval_seconds(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    value: int,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    await _insert_schedule(db_session_factory, kind="balance_scan")

    r = await http_client.patch(
        _SETTINGS_URL,
        json={"revision": "1", "schedules": {"balances": {field: value}}},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("field,value", [("freshness_seconds", 0), ("freshness_seconds", -1)])
async def test_patch_settings_rejects_non_positive_freshness_seconds(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
    field: str,
    value: int,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    await _insert_schedule(db_session_factory, kind="quote_refresh")

    r = await http_client.patch(
        _SETTINGS_URL,
        json={"revision": "1", "schedules": {"quotes": {field: value}}},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


async def test_patch_settings_accepts_positive_interval_seconds(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    await _insert_schedule(db_session_factory, kind="discovery")

    r = await http_client.patch(
        _SETTINGS_URL,
        json={"revision": "1", "schedules": {"discovery": {"interval_seconds": 30}}},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["schedules"]["discovery"]["interval_seconds"] == 30
    assert data["revision"] == "2"


async def test_patch_settings_rejects_before_applying_any_schedule(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An invalid schedule anywhere in the batch aborts the whole PATCH (no partial writes)."""
    csrf = await _setup_and_get_csrf(http_client)
    await _insert_schedule(db_session_factory, kind="balance_scan")
    await _insert_schedule(db_session_factory, kind="quote_refresh")

    r = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": "1",
            "schedules": {
                "balances": {"interval_seconds": 120},
                "quotes": {"interval_seconds": 0},
            },
        },
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422

    # balances must be untouched — the whole batch was rejected.
    r2 = await http_client.get(_SETTINGS_URL)
    assert r2.json()["schedules"]["balances"]["interval_seconds"] == 0
