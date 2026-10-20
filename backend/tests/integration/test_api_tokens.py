"""Integration tests for read-only API tokens (AUD-629).

AC coverage:
- Mutation with token → 403
- Route outside allow-list with token → 403
- Revoked token → 401
- Expired token → 401
- Invalid token → 401
- Integration secrets not reachable by token
- Token CRUD (list, create, revoke)
- Session-only routes still require session
- Valid token can read allowed routes
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.auth.service import TOKEN_PREFIX
from audr.db import get_db
from audr.operations.init_key import init_key

pytestmark = pytest.mark.integration

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_PASSWORD = "correct-horse-battery-staple-99"

_ALLOWED_ROUTES = [
    "/api/v1/portfolio",
    "/api/v1/portfolio/holdings",
    "/api/v1/assets",
    "/api/v1/history",
    "/api/v1/events",
    "/api/v1/allowances",
    "/api/v1/alerts",
    "/api/v1/status",
    "/api/v1/version",
]

_BLOCKED_ROUTES = [
    "/api/v1/integrations",
    "/api/v1/settings",
    "/api/v1/jobs",
    "/api/v1/exports/portfolio",
    "/api/v1/data/provider-purge-preview?provider=coingecko",
    "/api/v1/tokens",
]


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(sa.text("DELETE FROM token_attempt"))
            await session.execute(sa.text("DELETE FROM api_token"))
            await session.execute(sa.text("DELETE FROM job_run"))
            await session.execute(sa.text("DELETE FROM integration"))
            await session.execute(sa.text("DELETE FROM key_state"))
            await session.execute(sa.text("DELETE FROM login_attempt"))
            await session.execute(sa.text("DELETE FROM session"))
            await session.execute(sa.text("DELETE FROM owner"))
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


async def _setup_and_login(client: httpx.AsyncClient) -> str:
    """Create owner and return csrf_token."""
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


async def _mint_token_via_api(
    client: httpx.AsyncClient,
    csrf: str,
    name: str = "test-token",
    expires_days: int = 90,
    redact_addresses: bool = False,
) -> tuple[str, str]:
    """Create token via API; return (token_id, plaintext)."""
    r = await client.post(
        "/api/v1/tokens",
        json={"name": name, "expires_days": expires_days, "redact_addresses": redact_addresses},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    data = r.json()
    return data["id"], data["token"]


async def _add_wallet_via_api(
    client: httpx.AsyncClient, csrf: str, address: str, label: str = ""
) -> str:
    """Create a tracked wallet via API; return its id."""
    r = await client.post(
        "/api/v1/wallets",
        json={"address": address, "label": label},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Token CRUD
# ---------------------------------------------------------------------------


async def test_create_token_returns_plaintext_once(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    token_id, plaintext = await _mint_token_via_api(http_client, csrf)

    assert plaintext.startswith(TOKEN_PREFIX)
    assert len(plaintext) > len(TOKEN_PREFIX)

    # List tokens — plaintext is NOT returned in list
    r = await http_client.get("/api/v1/tokens")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == token_id
    assert "token" not in items[0]


async def test_list_tokens_requires_session(http_client: httpx.AsyncClient) -> None:
    # Not logged in at all
    r = await http_client.get("/api/v1/tokens")
    assert r.status_code == 401


async def test_create_token_requires_csrf(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)
    r = await http_client.post("/api/v1/tokens", json={"name": "x"})
    assert r.status_code == 403


async def test_revoke_token(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    token_id, plaintext = await _mint_token_via_api(http_client, csrf)

    # Token works before revoke
    r = await http_client.get(
        "/api/v1/portfolio",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 200

    # Revoke
    r2 = await http_client.delete(
        f"/api/v1/tokens/{token_id}",
        headers={"x-csrf-token": csrf},
    )
    assert r2.status_code == 204

    # Token is now rejected
    r3 = await http_client.get(
        "/api/v1/portfolio",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r3.status_code == 401


async def test_revoke_nonexistent_token_returns_404(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    import uuid

    r = await http_client.delete(
        f"/api/v1/tokens/{uuid.uuid4()}",
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 404


async def test_revoke_token_requires_csrf(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    token_id, _ = await _mint_token_via_api(http_client, csrf)
    r = await http_client.delete(f"/api/v1/tokens/{token_id}")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# AC: mutation with token → 403
# ---------------------------------------------------------------------------


async def test_post_with_token_is_forbidden(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)

    r = await http_client.post(
        "/api/v1/alerts/some-id/read",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 403


async def test_patch_with_token_is_forbidden(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)

    r = await http_client.patch(
        "/api/v1/settings",
        json={"revision": "0"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# AC: routes outside allow-list → 403
# ---------------------------------------------------------------------------


async def test_blocked_routes_return_403_with_token(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)
    # Otherwise the still-valid session cookie from setup/login would grant
    # these routes access on its own, masking whether the allow-list itself
    # actually blocks the token.
    http_client.cookies.clear()

    for route in _BLOCKED_ROUTES:
        r = await http_client.get(
            route,
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        # Session-only routes 401 (they never look at Authorization at all);
        # allow-list-checked routes 403. Both mean "a token can't read this".
        assert r.status_code in (401, 403), f"Expected 401/403 for {route}, got {r.status_code}"


# ---------------------------------------------------------------------------
# AC: revoked and expired token → 401
# ---------------------------------------------------------------------------


async def test_expired_token_returns_401(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)

    # Force-expire by setting expires_at in the past
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                sa.text(
                    "UPDATE api_token SET expires_at = :t WHERE token_hash = :h"
                ),
                {
                    "t": datetime.now(UTC) - timedelta(hours=1),
                    "h": __import__("hashlib").sha256(plaintext.encode()).hexdigest(),
                },
            )

    r = await http_client.get(
        "/api/v1/portfolio",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code == 401


async def test_invalid_token_returns_401(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)

    r = await http_client.get(
        "/api/v1/portfolio",
        headers={"Authorization": "Bearer audr_pat_this-is-not-real"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# AC: integration secrets not reachable by token
# ---------------------------------------------------------------------------


async def test_integrations_endpoint_not_reachable_by_token(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)
    http_client.cookies.clear()

    r = await http_client.get(
        "/api/v1/integrations",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code in (401, 403)


async def test_settings_endpoint_not_reachable_by_token(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)
    http_client.cookies.clear()

    r = await http_client.get(
        "/api/v1/settings",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Allowed routes respond 200 (or appropriate) with a valid token
# ---------------------------------------------------------------------------


async def test_allowed_routes_respond_with_token(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)

    # /version is unauthenticated, so no change needed — but token should work too
    r = await http_client.get(
        "/api/v1/version",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    # version is unauthenticated so it doesn't check the token at all — just 200
    assert r.status_code == 200

    for route in [
        "/api/v1/portfolio",
        "/api/v1/portfolio/holdings",
        "/api/v1/assets",
        "/api/v1/alerts",
        "/api/v1/status",
    ]:
        r = await http_client.get(
            route,
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert r.status_code == 200, f"Expected 200 for {route}, got {r.status_code}"


# ---------------------------------------------------------------------------
# Token endpoint itself is session-only (not in allow-list)
# ---------------------------------------------------------------------------


async def test_tokens_endpoint_not_accessible_with_token(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_login(http_client)
    _, plaintext = await _mint_token_via_api(http_client, csrf)
    http_client.cookies.clear()

    r = await http_client.get(
        "/api/v1/tokens",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    # Either 401 (route tries session, no cookie) or 403 (blocked by allow-list check)
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Revoked list shows only active tokens
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# redact_addresses flag masks wallet addresses in holdings responses
# ---------------------------------------------------------------------------


async def test_redact_addresses_masks_wallet_address_in_holdings(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from audr.portfolio.balances import record_balance

    csrf = await _setup_and_login(http_client)
    address = "0x" + "a" * 40
    token_address = "0x" + "b" * 40
    wallet_id = await _add_wallet_via_api(http_client, csrf, address, label="Cold storage")

    async with db_session_factory() as session:
        async with session.begin():
            await record_balance(
                session,
                wallet_address=address,
                token_address=token_address,
                raw_amount=1,
                block_number=1,
            )

    _, redacted_token = await _mint_token_via_api(
        http_client, csrf, name="redacted", redact_addresses=True
    )
    _, plain_token = await _mint_token_via_api(
        http_client, csrf, name="plain", redact_addresses=False
    )

    r = await http_client.get(
        f"/api/v1/wallets/{wallet_id}/holdings",
        headers={"Authorization": f"Bearer {redacted_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert address not in body["wallet_address"]
    assert "Cold storage" in body["wallet_address"]
    assert all(address not in h["wallet_address"] for h in body["holdings"])

    r2 = await http_client.get(
        f"/api/v1/wallets/{wallet_id}/holdings",
        headers={"Authorization": f"Bearer {plain_token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["wallet_address"] == address

    # Session auth is unaffected by any token's redact_addresses flag.
    r3 = await http_client.get(f"/api/v1/wallets/{wallet_id}/holdings")
    assert r3.status_code == 200
    assert r3.json()["wallet_address"] == address


async def test_revoked_token_disappears_from_list(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    token_id, _ = await _mint_token_via_api(http_client, csrf, name="to-revoke")
    await _mint_token_via_api(http_client, csrf, name="keep")

    r = await http_client.get("/api/v1/tokens")
    assert len(r.json()["items"]) == 2

    await http_client.delete(
        f"/api/v1/tokens/{token_id}",
        headers={"x-csrf-token": csrf},
    )

    r2 = await http_client.get("/api/v1/tokens")
    items = r2.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "keep"
