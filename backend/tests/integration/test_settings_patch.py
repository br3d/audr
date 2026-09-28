"""Integration tests for PATCH /settings optimistic locking (AUD-271).

Covers:
  - Concurrent PATCHes to the same schedule kind: second write must 409
    (CAS WHERE revision=:expected detects the lost-update race).
  - Concurrent PATCHes to *different* schedule kinds: both must succeed
    (per-kind CAS eliminates the spurious global-max conflict).
  - Stale revision from client: early global-max check still fires 409.

Requires a live PostgreSQL test database with migrations applied.
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

pytestmark = pytest.mark.integration

_BASE = "http://test"
_SETTINGS_URL = "/api/v1/settings"
_SETUP_URL = "/api/v1/setup"
_LOGIN_URL = "/api/v1/auth/login"
_PASSWORD = "hunter2-correct-horse-battery-42"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


@pytest.fixture(autouse=True)
async def _clean_tables(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Wipe auth tables and reset schedule revisions before every test."""
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))
            # Reset all schedules to a known revision so tests start clean.
            await session.execute(
                text("UPDATE schedule SET revision = 1, enabled = TRUE")
            )


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


async def _setup_and_login(client: httpx.AsyncClient) -> str:
    """Create owner, log in, and return CSRF token."""
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code in (201, 409), f"Unexpected setup status {r.status_code}"
    if r.status_code == 409:
        r = await client.post(_LOGIN_URL, json={"password": _PASSWORD})
        assert r.status_code == 200
    return r.json()["csrf_token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_patch_settings_stale_revision_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    """A PATCH with a revision that doesn't match the current max returns 409."""
    csrf = await _setup_and_login(http_client)

    # All schedules are at revision 1 (per fixture), so current max = 1.
    # Sending revision="99" is stale → should 409.
    r = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": "99",
            "schedules": {"balances": {"enabled": True}},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 409, r.text


async def test_patch_settings_valid_revision_succeeds(
    http_client: httpx.AsyncClient,
) -> None:
    """A PATCH with the correct current revision succeeds."""
    csrf = await _setup_and_login(http_client)

    # Fetch current revision.
    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 200
    revision = r.json()["revision"]

    r = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": revision,
            "schedules": {"balances": {"enabled": False}},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert r.json()["schedules"]["balances"]["enabled"] is False


async def test_patch_settings_cas_catches_concurrent_same_kind(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two PATCHes to the same kind with the same revision: second must 409.

    This exercises the CAS WHERE revision=:expected guard — it must detect
    the lost-update race even when both requests pass the early global check.
    """
    csrf = await _setup_and_login(http_client)

    # Fetch current revision.
    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 200
    revision = r.json()["revision"]

    # First PATCH — succeeds, bumps balances revision.
    r1 = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": revision,
            "schedules": {"balances": {"enabled": False}},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200, f"First PATCH failed: {r1.text}"

    # Second PATCH uses the SAME stale revision — the CAS guard must reject it.
    r2 = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": revision,
            "schedules": {"balances": {"enabled": True}},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 409, (
        f"Expected 409 for stale-revision concurrent write, got {r2.status_code}: {r2.text}"
    )


async def test_patch_settings_cas_different_kinds_no_spurious_conflict(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After one kind is updated, a fresh PATCH to a *different* kind succeeds.

    Verifies that the per-kind CAS doesn't create spurious conflicts when
    unrelated kinds are edited sequentially from an up-to-date revision.
    """
    csrf = await _setup_and_login(http_client)

    # Fetch current revision.
    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 200
    revision = r.json()["revision"]

    # PATCH balances only.
    r1 = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": revision,
            "schedules": {"balances": {"enabled": False}},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r1.status_code == 200, f"Balances PATCH failed: {r1.text}"
    new_revision = r1.json()["revision"]

    # PATCH discovery using the NEW revision (re-fetched after first write).
    r2 = await http_client.patch(
        _SETTINGS_URL,
        json={
            "revision": new_revision,
            "schedules": {"discovery": {"enabled": False}},
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200, (
        f"Discovery PATCH with up-to-date revision should succeed, got {r2.status_code}: {r2.text}"
    )


# ---------------------------------------------------------------------------
# CSRF enforcement tests
# ---------------------------------------------------------------------------


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
