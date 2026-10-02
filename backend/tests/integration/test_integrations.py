"""Integration tests for the integrations API (AUD-290).

Covers:
  - GET /api/v1/integrations: returns both rpc and quotes entries (unconfigured)
  - GET /api/v1/integrations: no CSRF required (session-only)
  - GET /api/v1/integrations: 401 without session
  - PUT /api/v1/integrations/rpc: saves URL, returns IntegrationEntry
  - PUT /api/v1/integrations/rpc: 422 on invalid URL
  - PUT /api/v1/integrations/rpc: 409 on revision conflict
  - PUT /api/v1/integrations/quotes: saves credentials
  - POST /api/v1/integrations/rpc/validate: queues a validate_rpc job
  - POST /api/v1/integrations/unknown/validate: 404
  - Auth fix: GET /wallets returns 200 without CSRF token (session-only)
  - Auth fix: GET /settings returns 200 without CSRF token (session-only)
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

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_INTEGRATIONS_URL = "/api/v1/integrations"
_WALLETS_URL = "/api/v1/wallets"
_SETTINGS_URL = "/api/v1/settings"
_PASSWORD = "correct-horse-battery-staple-99"

# A valid public-style RPC URL (will pass URL format validation; private hosts blocked by default)
_VALID_RPC_URL = "https://mainnet.infura.io/v3/test-key"


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM job_run"))
            await session.execute(text("DELETE FROM integration"))
            await session.execute(text("DELETE FROM key_state"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))
    # Re-initialise master key after clearing key_state.
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


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


# ---------------------------------------------------------------------------
# GET /integrations
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_integrations_returns_both_kinds(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_INTEGRATIONS_URL)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "request_id" in data
    assert "generated_at" in data
    kinds = {item["kind"] for item in data["items"]}
    assert kinds == {"rpc", "quotes"}


@pytest.mark.integration
async def test_get_integrations_unconfigured_entries(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_INTEGRATIONS_URL)
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["configured"] is False
        assert item["health"]["status"] == "unvalidated"
        assert item["revision"] == "0"


@pytest.mark.integration
async def test_get_integrations_no_csrf_needed(http_client: httpx.AsyncClient) -> None:
    """GET /integrations must not require CSRF — only a valid session cookie."""
    await _setup_and_get_csrf(http_client)  # sets session cookie; we ignore the csrf_token
    r = await http_client.get(_INTEGRATIONS_URL)
    assert r.status_code == 200


@pytest.mark.integration
async def test_get_integrations_requires_session(http_client: httpx.AsyncClient) -> None:
    r = await http_client.get(_INTEGRATIONS_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# PUT /integrations/rpc
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_put_rpc_saves_url(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": _VALID_RPC_URL},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "rpc"
    assert data["configured"] is True
    assert data["revision"] == "1"
    assert data["host_label"] == "mainnet.infura.io"


@pytest.mark.integration
async def test_put_rpc_invalid_url_returns_422(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": "ftp://bad-scheme.com"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.integration
async def test_put_rpc_revision_conflict_returns_409(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    # First save succeeds → revision becomes 1.
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": _VALID_RPC_URL},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    # Attempt second save with stale revision 0 → 409.
    r2 = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": _VALID_RPC_URL},
        headers={"x-csrf-token": csrf},
    )
    assert r2.status_code == 409


@pytest.mark.integration
async def test_put_rpc_requires_csrf(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": _VALID_RPC_URL},
    )
    assert r.status_code == 403


@pytest.mark.integration
async def test_put_rpc_private_host_rejected_by_default(
    http_client: httpx.AsyncClient,
) -> None:
    """AUD-322: a private/loopback RPC URL is rejected unless explicitly allowed."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": "http://127.0.0.1:8545/"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


@pytest.mark.integration
async def test_put_rpc_allow_private_host_persists_for_worker_revalidation(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AUD-322: allow_private_host is stored, not just used once at save time —
    the worker re-validates the stored URL before every use and must honour
    the same policy the owner explicitly opted into."""
    from audr.providers.rpc_targets import get_validated_rpc_url

    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={
            "revision": "0",
            "url": "http://127.0.0.1:8545/",
            "allow_private_host": True,
        },
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    async with db_session_factory() as session:
        revalidated = await get_validated_rpc_url(session)
    assert revalidated == "http://127.0.0.1:8545"


@pytest.mark.integration
async def test_get_validated_rpc_url_rejects_stale_unallowed_private_url(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    """AUD-322: re-validation at use time rejects a private URL that was never
    explicitly allowed, even if it somehow ended up stored (e.g. DNS rebinding
    after the integration was saved against a then-public hostname)."""
    from audr.providers.rpc_targets import RpcUrlError, get_validated_rpc_url
    from audr.settings.integrations import upsert_integration

    async with db_session_factory() as session:
        async with session.begin():
            await upsert_integration(
                session,
                kind="rpc",
                url="http://127.0.0.1:8545/",
                allow_private_host=False,
            )

    async with db_session_factory() as session:
        with pytest.raises(RpcUrlError):
            await get_validated_rpc_url(session)


# ---------------------------------------------------------------------------
# PUT /integrations/quotes
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_put_quotes_saves_credentials(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/quotes",
        json={"revision": "0", "provider": "coingecko", "api_key": "cg-test-key"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "quotes"
    assert data["configured"] is True
    assert data["provider"] == "coingecko"
    assert data["revision"] == "1"


@pytest.mark.integration
async def test_put_quotes_unsupported_provider_returns_422(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/quotes",
        json={"revision": "0", "provider": "binance"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /integrations/{kind}/validate
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_validate_rpc_queues_job(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        f"{_INTEGRATIONS_URL}/rpc/validate",
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202
    data = r.json()
    assert "run_id" in data
    assert data["coalesced"] is False


@pytest.mark.integration
async def test_validate_unknown_kind_returns_404(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        f"{_INTEGRATIONS_URL}/unknown/validate",
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Defect 1 regression: GET /wallets must not require CSRF
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_wallets_no_csrf_needed(http_client: httpx.AsyncClient) -> None:
    """GET /wallets must return 200 with only a session cookie (no X-CSRF-Token)."""
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_WALLETS_URL)
    assert r.status_code == 200


@pytest.mark.integration
async def test_get_wallets_requires_session(http_client: httpx.AsyncClient) -> None:
    r = await http_client.get(_WALLETS_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Defect 2 regression: GET /settings must not require CSRF
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_get_settings_no_csrf_needed(http_client: httpx.AsyncClient) -> None:
    """GET /settings must return 200 with only a session cookie (no X-CSRF-Token)."""
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 200


@pytest.mark.integration
async def test_get_settings_requires_session(http_client: httpx.AsyncClient) -> None:
    r = await http_client.get(_SETTINGS_URL)
    assert r.status_code == 401
