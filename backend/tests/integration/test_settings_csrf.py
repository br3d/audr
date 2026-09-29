"""CSRF enforcement tests for state-mutating settings/jobs endpoints (AUD-277).

Covers:
  - PATCH /settings without X-CSRF-Token: 403
  - POST /jobs without X-CSRF-Token: 403
  - POST /data/provider-purge without X-CSRF-Token: 403
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db
from audr.operations.init_key import init_key

pytestmark = pytest.mark.integration

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_SETTINGS_URL = "/api/v1/settings"
_PASSWORD = "correct-horse-battery-staple-99"


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM schedule"))
            await session.execute(text("DELETE FROM key_state"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))
    async with db_session_factory() as session:
        async with session.begin():
            await init_key(session)


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


async def _setup_and_login(client: httpx.AsyncClient) -> None:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201


async def test_patch_settings_without_csrf_token_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    """PATCH /settings without an X-CSRF-Token header must return 403."""
    await _setup_and_login(http_client)

    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 200
    revision = r.json()["revision"]

    r = await http_client.patch(
        _SETTINGS_URL,
        json={"revision": revision, "schedules": {"balances": {"enabled": False}}},
        # No X-CSRF-Token header
    )
    assert r.status_code == 403, f"Expected 403 without CSRF token, got {r.status_code}: {r.text}"


async def test_post_jobs_without_csrf_token_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    """POST /jobs without an X-CSRF-Token header must return 403."""
    await _setup_and_login(http_client)

    r = await http_client.post(
        "/api/v1/jobs",
        json={"kind": "balances"},
        # No X-CSRF-Token header
    )
    assert r.status_code == 403, f"Expected 403 without CSRF token, got {r.status_code}: {r.text}"


async def test_post_provider_purge_without_csrf_token_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    """POST /data/provider-purge without an X-CSRF-Token header must return 403."""
    await _setup_and_login(http_client)

    r = await http_client.post(
        "/api/v1/data/provider-purge",
        json={"provider": "coingecko", "confirm": True, "current_password": _PASSWORD},
        # No X-CSRF-Token header
    )
    assert r.status_code == 403, f"Expected 403 without CSRF token, got {r.status_code}: {r.text}"
