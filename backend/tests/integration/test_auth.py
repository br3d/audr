"""Integration tests for authentication routes (T021).

Covers:
  - Concurrent setup: only one of two simultaneous POST /setup requests succeeds.
  - Authentication: setup/login/session/logout flows.
  - Session expiry: expired sessions return 401.
  - CSRF/origin: missing or wrong CSRF token → 403; wrong Origin → 403.
  - Password change: PATCH /auth/password updates hash and revokes sessions.
  - Persistent login throttle: too many failures → 429 with Retry-After.

Requires a live PostgreSQL test database with migrations applied through 002.
"""

from __future__ import annotations

import asyncio
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
_PASSWORD = "correct-horse-battery-staple-99"
_SETUP_URL = "/api/v1/setup"
_STATUS_URL = "/api/v1/setup/status"
_LOGIN_URL = "/api/v1/auth/login"
_SESSION_URL = "/api/v1/auth/session"
_LOGOUT_URL = "/api/v1/auth/logout"
_PASSWORD_URL = "/api/v1/auth/password"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_auth_tables(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Truncate auth tables before every test; committed so the HTTP client sees them empty."""
    async with db_session_factory() as session:
        async with session.begin():
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
    """AsyncClient wired to the ASGI app with the test database injected."""
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


async def _do_setup(client: httpx.AsyncClient, password: str = _PASSWORD) -> httpx.Response:
    return await client.post(_SETUP_URL, json={"password": password})


async def _do_login(client: httpx.AsyncClient, password: str = _PASSWORD) -> httpx.Response:
    return await client.post(_LOGIN_URL, json={"password": password})


async def _csrf_from(response: httpx.Response) -> str:
    return response.json()["csrf_token"]


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await _do_setup(client)
    assert r.status_code == 201
    return await _csrf_from(r)


async def _login_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await _do_login(client)
    assert r.status_code == 200
    return await _csrf_from(r)


# ---------------------------------------------------------------------------
# Concurrent setup tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_concurrent_setup_only_one_succeeds(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two simultaneous POST /setup calls must result in exactly one 201 and one 409."""
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as c1:
            async with httpx.AsyncClient(transport=transport, base_url=_BASE) as c2:
                r1, r2 = await asyncio.gather(
                    _do_setup(c1),
                    _do_setup(c2),
                )
    finally:
        app.dependency_overrides.pop(get_db, None)

    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [201, 409], f"Expected [201, 409], got {statuses}"


@pytest.mark.integration
async def test_second_setup_returns_409(http_client: httpx.AsyncClient) -> None:
    """POST /setup after owner exists must return 409."""
    r1 = await _do_setup(http_client)
    assert r1.status_code == 201
    r2 = await _do_setup(http_client)
    assert r2.status_code == 409


# ---------------------------------------------------------------------------
# Authentication flow tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_setup_status_no_owner(http_client: httpx.AsyncClient) -> None:
    """GET /setup/status returns setup_required=true when no owner exists."""
    r = await http_client.get(_STATUS_URL)
    assert r.status_code == 200
    assert r.json()["setup_required"] is True


@pytest.mark.integration
async def test_setup_status_owner_exists(http_client: httpx.AsyncClient) -> None:
    """GET /setup/status returns setup_required=false after setup."""
    await _do_setup(http_client)
    r = await http_client.get(_STATUS_URL)
    assert r.status_code == 200
    assert r.json()["setup_required"] is False


@pytest.mark.integration
async def test_setup_returns_csrf_token(http_client: httpx.AsyncClient) -> None:
    """POST /setup returns a non-empty csrf_token."""
    r = await _do_setup(http_client)
    assert r.status_code == 201
    data = r.json()
    assert "csrf_token" in data
    assert len(data["csrf_token"]) > 0


@pytest.mark.integration
async def test_setup_sets_session_cookie(http_client: httpx.AsyncClient) -> None:
    """POST /setup sets an HttpOnly sid cookie."""
    r = await _do_setup(http_client)
    assert r.status_code == 201
    assert "sid" in r.cookies


@pytest.mark.integration
async def test_login_correct_password(http_client: httpx.AsyncClient) -> None:
    """POST /auth/login with correct password returns 200 and csrf_token."""
    await _do_setup(http_client)
    r = await _do_login(http_client)
    assert r.status_code == 200
    assert "csrf_token" in r.json()


@pytest.mark.integration
async def test_login_wrong_password_returns_401(http_client: httpx.AsyncClient) -> None:
    """POST /auth/login with wrong password returns 401."""
    await _do_setup(http_client)
    r = await _do_login(http_client, password="totally-wrong-password-!!")
    assert r.status_code == 401


@pytest.mark.integration
async def test_login_before_setup_returns_401(http_client: httpx.AsyncClient) -> None:
    """POST /auth/login before any owner exists returns 401."""
    r = await _do_login(http_client)
    assert r.status_code == 401


@pytest.mark.integration
async def test_session_endpoint_requires_auth(http_client: httpx.AsyncClient) -> None:
    """GET /auth/session without a session cookie returns 401."""
    r = await http_client.get(_SESSION_URL)
    assert r.status_code == 401


@pytest.mark.integration
async def test_session_endpoint_authenticated(http_client: httpx.AsyncClient) -> None:
    """GET /auth/session with a valid session returns 200 with authenticated=true."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.get(_SESSION_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["authenticated"] is True
    assert "expires_at" in data
    assert data["csrf_token"] == csrf


@pytest.mark.integration
async def test_logout_revokes_session(http_client: httpx.AsyncClient) -> None:
    """POST /auth/logout revokes the session; subsequent GET /auth/session returns 401."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(_LOGOUT_URL, headers={"x-csrf-token": csrf})
    assert r.status_code == 204

    r2 = await http_client.get(_SESSION_URL)
    assert r2.status_code == 401


# ---------------------------------------------------------------------------
# Session expiry tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_expired_session_returns_401(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session whose expires_at is in the past must be rejected with 401."""
    await _do_setup(http_client)

    # Backdate expires_at for all sessions.
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE session SET expires_at = now() - interval '1 second'"
                )
            )

    r = await http_client.get(_SESSION_URL)
    assert r.status_code == 401


@pytest.mark.integration
async def test_active_session_not_expired(http_client: httpx.AsyncClient) -> None:
    """A freshly-created session is not expired and is accessible."""
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_SESSION_URL)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# CSRF / origin tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_mutation_without_csrf_token_rejected(http_client: httpx.AsyncClient) -> None:
    """POST /auth/logout without X-CSRF-Token returns 403."""
    await _do_setup(http_client)
    r = await http_client.post(_LOGOUT_URL)
    assert r.status_code == 403


@pytest.mark.integration
async def test_mutation_with_wrong_csrf_token_rejected(http_client: httpx.AsyncClient) -> None:
    """POST /auth/logout with an incorrect X-CSRF-Token returns 403."""
    await _do_setup(http_client)
    r = await http_client.post(_LOGOUT_URL, headers={"x-csrf-token": "deadbeef"})
    assert r.status_code == 403


@pytest.mark.integration
async def test_mutation_with_correct_csrf_token_succeeds(http_client: httpx.AsyncClient) -> None:
    """POST /auth/logout with the correct X-CSRF-Token succeeds (204)."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(_LOGOUT_URL, headers={"x-csrf-token": csrf})
    assert r.status_code == 204


@pytest.mark.integration
async def test_cross_origin_mutation_rejected(http_client: httpx.AsyncClient) -> None:
    """POST mutation with Origin header not matching Host must return 403."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        _LOGOUT_URL,
        headers={
            "x-csrf-token": csrf,
            "origin": "http://attacker.example.com",
        },
    )
    assert r.status_code == 403


@pytest.mark.integration
async def test_same_origin_mutation_succeeds(http_client: httpx.AsyncClient) -> None:
    """POST mutation with matching Origin header (same as Host) is allowed."""
    csrf = await _setup_and_get_csrf(http_client)
    # base_url is http://test so Host=test; send Origin: http://test
    r = await http_client.post(
        _LOGOUT_URL,
        headers={
            "x-csrf-token": csrf,
            "origin": "http://test",
        },
    )
    assert r.status_code == 204


# ---------------------------------------------------------------------------
# Password change tests
# ---------------------------------------------------------------------------

_NEW_PASSWORD = "new-correct-horse-battery-99"


@pytest.mark.integration
async def test_change_password_succeeds(http_client: httpx.AsyncClient) -> None:
    """PATCH /auth/password with correct current password returns 204."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.patch(
        _PASSWORD_URL,
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 204


@pytest.mark.integration
async def test_change_password_wrong_current_returns_403(http_client: httpx.AsyncClient) -> None:
    """PATCH /auth/password with wrong current password returns 403."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.patch(
        _PASSWORD_URL,
        json={"current_password": "wrong-password-!!!!", "new_password": _NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 403


@pytest.mark.integration
async def test_change_password_invalidates_sessions(http_client: httpx.AsyncClient) -> None:
    """After a password change all sessions are revoked; old session returns 401."""
    csrf = await _setup_and_get_csrf(http_client)

    # Change password.
    r = await http_client.patch(
        _PASSWORD_URL,
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 204

    # Old session cookie is still in the client jar but the DB row is revoked.
    r2 = await http_client.get(_SESSION_URL)
    assert r2.status_code == 401


@pytest.mark.integration
async def test_change_password_new_password_works(http_client: httpx.AsyncClient) -> None:
    """After a password change the new password can be used to login."""
    csrf = await _setup_and_get_csrf(http_client)
    await http_client.patch(
        _PASSWORD_URL,
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    # Login with new password.
    r = await _do_login(http_client, password=_NEW_PASSWORD)
    assert r.status_code == 200


@pytest.mark.integration
async def test_change_password_old_password_rejected(http_client: httpx.AsyncClient) -> None:
    """After a password change the old password no longer works."""
    csrf = await _setup_and_get_csrf(http_client)
    await http_client.patch(
        _PASSWORD_URL,
        json={"current_password": _PASSWORD, "new_password": _NEW_PASSWORD},
        headers={"x-csrf-token": csrf},
    )
    r = await _do_login(http_client, password=_PASSWORD)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Persistent login throttle tests
# ---------------------------------------------------------------------------

from audr.auth.service import THROTTLE_MAX_FAILURES  # noqa: E402


@pytest.mark.integration
async def test_throttle_activates_after_max_failures(http_client: httpx.AsyncClient) -> None:
    """After THROTTLE_MAX_FAILURES failed attempts POST /auth/login returns 429."""
    await _do_setup(http_client)

    for _ in range(THROTTLE_MAX_FAILURES):
        r = await _do_login(http_client, password="wrong-password-!!!!!!")
        assert r.status_code == 401

    r = await _do_login(http_client, password="wrong-password-!!!!!!")
    assert r.status_code == 429


@pytest.mark.integration
async def test_throttle_response_includes_retry_after(http_client: httpx.AsyncClient) -> None:
    """A 429 response from the login endpoint must include a Retry-After header."""
    await _do_setup(http_client)

    for _ in range(THROTTLE_MAX_FAILURES):
        await _do_login(http_client, password="wrong-!!!!!!!!!!!!!!!")

    r = await _do_login(http_client, password="wrong-!!!!!!!!!!!!!!!")
    assert r.status_code == 429
    assert "retry-after" in r.headers


@pytest.mark.integration
async def test_throttle_blocks_correct_password(http_client: httpx.AsyncClient) -> None:
    """Once throttled, even the correct password is rejected with 429."""
    await _do_setup(http_client)

    for _ in range(THROTTLE_MAX_FAILURES):
        await _do_login(http_client, password="wrong-!!!!!!!!!!!!!!!")

    r = await _do_login(http_client, password=_PASSWORD)
    assert r.status_code == 429


@pytest.mark.integration
async def test_throttle_resets_after_window(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Throttle clears when all failed attempts are older than the throttle window."""
    await _do_setup(http_client)

    for _ in range(THROTTLE_MAX_FAILURES):
        await _do_login(http_client, password="wrong-!!!!!!!!!!!!!!!")

    # Expire all failed attempts.
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE login_attempt SET attempted_at = now() - interval '20 minutes'"
                    " WHERE success = false"
                )
            )

    # Login should now succeed.
    r = await _do_login(http_client, password=_PASSWORD)
    assert r.status_code == 200


@pytest.mark.integration
async def test_successful_login_does_not_count_toward_throttle(
    http_client: httpx.AsyncClient,
) -> None:
    """A successful login does not increment the failure counter."""
    await _do_setup(http_client)

    # Almost at the threshold.
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        await _do_login(http_client, password="wrong-!!!!!!!!!!!!!!!")

    # One successful login.
    r = await _do_login(http_client, password=_PASSWORD)
    assert r.status_code == 200

    # The next attempt with the correct password should still succeed (not throttled).
    r2 = await _do_login(http_client, password=_PASSWORD)
    assert r2.status_code == 200
