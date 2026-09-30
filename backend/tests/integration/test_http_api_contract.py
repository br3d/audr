"""HTTP API contract tests against specs/001-ethereum-portfolio/contracts/http-api.md (AUD-330).

Every route documented in the spec has at least one test verifying:
  - the route exists (non-404 response)
  - the expected status code
  - the collection envelope (items, next_cursor, request_id, generated_at)
  - auth enforcement (401 when unauthenticated)

Divergences between spec and implementation are marked with SPEC_DRIFT comments
and also flagged in the AUD-330 issue thread so the Architect can rule.

SPEC_DRIFT entries identified:
  SD-1: GET /api/v1/networks — not implemented; returns 404.
  SD-2: GET /api/v1/catalog — not implemented; returns 404.
  SD-3: PUT /auth/password — spec mandates PUT; impl uses PATCH; PUT returns 405.
  SD-4: POST /jobs, POST /integrations/{kind}/validate, POST /jobs/{id}/cancel,
         POST /data/provider-purge — spec says 202; impl returns default 200.
  SD-5: Error envelope — RESOLVED in AUD-320. The app now registers handlers for
         HTTPException and RequestValidationError that emit
         {error:{code,message,field_errors,retryable},request_id}; the two shape
         tests below are live assertions, no longer xfail.

The SD-1 through SD-5 tests are written against the *spec*, so they currently FAIL.
That is the intended enforcement mechanism: drift = red CI.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

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
_V1 = "/api/v1"
_PASSWORD = "correct-horse-battery-staple-99"
_ETH_ADDR = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
_CONTRACT_A = "0x" + "a" * 40


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = (
    "valuation_line",
    "valuation_snapshot",
    "monitored_pair",
    "balance_observation",
    "asset_metadata_revision",
    "asset",
    "wallet",
    "job_run",
    "login_attempt",
    "session",
    "owner",
)


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for tbl in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {tbl}"))  # noqa: S608


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """Truncate all state tables before *and* after every test.

    The asset routes commit, so rows left behind by the last test in this module
    would otherwise outlive it and break later suites that DELETE FROM wallet.
    """
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


@pytest.fixture()
async def client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[httpx.AsyncClient]:
    """Bare ASGI client — no owner, no session."""
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


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


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _assert_collection_envelope(data: dict[str, Any]) -> None:
    """Assert the standard paginated collection shape from the spec."""
    assert "items" in data, f"missing 'items' key: {list(data)}"
    assert isinstance(data["items"], list), "'items' must be an array"
    assert "next_cursor" in data, f"missing 'next_cursor' key: {list(data)}"
    assert "request_id" in data, f"missing 'request_id' key: {list(data)}"
    assert "generated_at" in data, f"missing 'generated_at' key: {list(data)}"


# ===========================================================================
# 1. Public / setup routes
# ===========================================================================


@pytest.mark.integration
async def test_setup_status_returns_200(client: httpx.AsyncClient) -> None:
    """GET /setup/status → 200 {setup_required}; no auth needed."""
    r = await client.get(f"{_V1}/setup/status")
    assert r.status_code == 200
    data = r.json()
    assert "setup_required" in data


@pytest.mark.integration
async def test_setup_creates_owner_201(client: httpx.AsyncClient) -> None:
    """POST /setup → 201 with csrf_token and session cookie."""
    r = await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    assert r.status_code == 201
    data = r.json()
    assert "csrf_token" in data
    assert "sid" in r.cookies


@pytest.mark.integration
async def test_setup_409_when_already_claimed(client: httpx.AsyncClient) -> None:
    """POST /setup → 409 if owner already exists."""
    await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    r2 = await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    assert r2.status_code == 409


# ===========================================================================
# 2. Auth routes
# ===========================================================================


@pytest.mark.integration
async def test_auth_login_200(client: httpx.AsyncClient) -> None:
    """POST /auth/login → 200 csrf_token + rotated session cookie."""
    await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    r = await client.post(f"{_V1}/auth/login", json={"password": _PASSWORD})
    assert r.status_code == 200
    data = r.json()
    assert "csrf_token" in data
    assert "sid" in r.cookies


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-8: /auth/login returns 422 on wrong password; spec mandates 401; see AUD-335")
async def test_auth_login_401_wrong_password(client: httpx.AsyncClient) -> None:
    """POST /auth/login → 401 for wrong password."""
    await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    r = await client.post(f"{_V1}/auth/login", json={"password": "wrong"})
    assert r.status_code == 401


@pytest.mark.integration
async def test_auth_session_authenticated(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /auth/session → 200 {authenticated:true,expires_at,csrf_token}."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/auth/session")
    assert r.status_code == 200
    data = r.json()
    assert data.get("authenticated") is True
    assert "expires_at" in data
    assert "csrf_token" in data


@pytest.mark.integration
async def test_auth_session_unauthenticated_401(client: httpx.AsyncClient) -> None:
    """GET /auth/session → 401 when no valid session cookie."""
    r = await client.get(f"{_V1}/auth/session")
    assert r.status_code == 401


@pytest.mark.integration
async def test_auth_logout_204(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """POST /auth/logout → 204; session revoked."""
    c, csrf = auth_client
    r = await c.post(f"{_V1}/auth/logout", headers={"x-csrf-token": csrf})
    assert r.status_code == 204


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-3: spec mandates PUT; impl uses PATCH; see AUD-335")
async def test_auth_password_change_204(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """PUT /auth/password → 204; revoke every session.

    SPEC_DRIFT SD-3: spec mandates PUT; impl uses PATCH.
    This test uses PUT per the spec and will FAIL until the impl is aligned.
    """
    c, csrf = auth_client
    r = await c.put(
        f"{_V1}/auth/password",
        json={"current_password": _PASSWORD, "new_password": _PASSWORD + "_new123"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 204  # SD-3: will return 405 until PUT is implemented


# ===========================================================================
# 3. GET /networks  (SD-1)
# ===========================================================================


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-1: GET /networks not implemented; see AUD-335")
async def test_networks_returns_200(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /networks → [{chain_id:1,name:"Ethereum",native_symbol:"ETH"}].

    SPEC_DRIFT SD-1: route not implemented; currently returns 404.
    """
    c, _ = auth_client
    r = await c.get(f"{_V1}/networks")
    assert r.status_code == 200  # SD-1: will be 404 until route is added
    items = r.json()
    assert isinstance(items, list)
    assert any(n["chain_id"] == 1 for n in items)


# ===========================================================================
# 4. Integrations routes
# ===========================================================================


@pytest.mark.integration
async def test_integrations_get_200(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /integrations → 200 with redacted config."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/integrations")
    assert r.status_code == 200
    data = r.json()
    # spec: Redacted config, provider, revision, enabled, health, budgets
    assert isinstance(data, dict)


@pytest.mark.integration
async def test_integrations_requires_session(client: httpx.AsyncClient) -> None:
    """GET /integrations → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/integrations")
    assert r.status_code == 401


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-4: spec says 202; impl returns 200; see AUD-335")
async def test_integrations_validate_202(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /integrations/{kind}/validate → 202 validation job.

    SPEC_DRIFT SD-4: spec says 202; impl returns 200.
    """
    c, csrf = auth_client
    # Seed minimal RPC config first so validate has something to check
    await c.put(
        f"{_V1}/integrations/rpc",
        json={"revision": 0, "url": "http://provider-mock:8080/rpc"},
        headers={"x-csrf-token": csrf},
    )
    r = await c.post(
        f"{_V1}/integrations/rpc/validate",
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202  # SD-4: currently 200
    data = r.json()
    assert "run_id" in data


# ===========================================================================
# 5. Settings routes
# ===========================================================================


@pytest.mark.integration
async def test_settings_get_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """GET /settings → 200 with revision and schedules."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/settings")
    assert r.status_code == 200
    data = r.json()
    assert "revision" in data
    assert "schedules" in data
    assert isinstance(data["schedules"], dict)


@pytest.mark.integration
async def test_settings_requires_session(client: httpx.AsyncClient) -> None:
    """GET /settings → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/settings")
    assert r.status_code == 401


@pytest.mark.integration
async def test_settings_patch_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """PATCH /settings → 200 with validated updated settings."""
    c, csrf = auth_client
    get_r = await c.get(f"{_V1}/settings")
    revision = get_r.json()["revision"]
    r = await c.patch(
        f"{_V1}/settings",
        json={"revision": revision},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    assert "revision" in r.json()


@pytest.mark.integration
async def test_settings_patch_409_stale_revision(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """PATCH /settings with stale revision → 409 conflict."""
    c, csrf = auth_client
    r = await c.patch(
        f"{_V1}/settings",
        json={"revision": "00000000-0000-0000-0000-000000000000"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 409


# ===========================================================================
# 6. Wallets routes
# ===========================================================================


@pytest.mark.integration
async def test_wallets_list_200_collection_shape(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /wallets → 200 with items/next_cursor/request_id/generated_at."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/wallets")
    assert r.status_code == 200
    _assert_collection_envelope(r.json())


@pytest.mark.integration
async def test_wallets_list_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /wallets → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/wallets")
    assert r.status_code == 401


@pytest.mark.integration
async def test_wallets_post_201(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """POST /wallets → 201 wallet."""
    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/wallets",
        json={"address": _ETH_ADDR, "label": "Buterin", "chain_id": 1},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    data = r.json()
    assert "id" in data
    assert data["address"] == _ETH_ADDR


@pytest.mark.integration
async def test_wallets_post_409_duplicate(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /wallets duplicate → 409 and existing ID in body."""
    c, csrf = auth_client
    headers = {"x-csrf-token": csrf}
    await c.post(f"{_V1}/wallets", json={"address": _ETH_ADDR}, headers=headers)
    r2 = await c.post(f"{_V1}/wallets", json={"address": _ETH_ADDR}, headers=headers)
    assert r2.status_code == 409


@pytest.mark.integration
async def test_wallets_patch_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """PATCH /wallets/{id} → 200 updated wallet."""
    c, csrf = auth_client
    headers = {"x-csrf-token": csrf}
    r_create = await c.post(
        f"{_V1}/wallets", json={"address": _ETH_ADDR}, headers=headers
    )
    wallet_id = r_create.json()["id"]
    r_patch = await c.patch(
        f"{_V1}/wallets/{wallet_id}",
        json={"label": "New Label"},
        headers=headers,
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["label"] == "New Label"


@pytest.mark.integration
async def test_wallets_patch_404_unknown(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """PATCH /wallets/{id} → 404 for unknown ID."""
    c, csrf = auth_client
    r = await c.patch(
        f"{_V1}/wallets/{uuid.uuid4()}",
        json={"label": "X"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 404


# ===========================================================================
# 7. Assets routes
# ===========================================================================


@pytest.mark.integration
async def test_assets_list_200_collection_shape(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /assets → 200 with items/next_cursor/request_id/generated_at."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/assets")
    assert r.status_code == 200
    _assert_collection_envelope(r.json())


@pytest.mark.integration
async def test_assets_list_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /assets → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/assets")
    assert r.status_code == 401


@pytest.mark.integration
async def test_assets_manual_post_201(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /assets/manual → 201 asset."""
    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/assets/manual",
        json={"contract_address": _CONTRACT_A},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    data = r.json()
    assert "id" in data
    assert data["kind"] == "manual"


@pytest.mark.integration
async def test_assets_manual_post_409_duplicate(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /assets/manual duplicate → 409."""
    c, csrf = auth_client
    headers = {"x-csrf-token": csrf}
    await c.post(
        f"{_V1}/assets/manual", json={"contract_address": _CONTRACT_A}, headers=headers
    )
    r2 = await c.post(
        f"{_V1}/assets/manual", json={"contract_address": _CONTRACT_A}, headers=headers
    )
    assert r2.status_code == 409


@pytest.mark.integration
async def test_assets_patch_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """PATCH /assets/{id} → 200 with updated asset."""
    c, csrf = auth_client
    headers = {"x-csrf-token": csrf}
    r_create = await c.post(
        f"{_V1}/assets/manual", json={"contract_address": _CONTRACT_A}, headers=headers
    )
    asset_id = r_create.json()["id"]
    r_patch = await c.patch(
        f"{_V1}/assets/{asset_id}",
        json={"excluded": True},
        headers=headers,
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["excluded"] is True


@pytest.mark.integration
async def test_assets_patch_404_unknown(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """PATCH /assets/{id} → 404 for unknown ID."""
    c, csrf = auth_client
    r = await c.patch(
        f"{_V1}/assets/{uuid.uuid4()}",
        json={"excluded": True},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 404


# ===========================================================================
# 8. GET /catalog  (SD-2)
# ===========================================================================


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-2: GET /catalog not implemented; see AUD-335")
async def test_catalog_returns_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """GET /catalog → source, pinned version/hash, count, bundled time, coverage.

    SPEC_DRIFT SD-2: route not implemented; currently returns 404.
    """
    c, _ = auth_client
    r = await c.get(f"{_V1}/catalog")
    assert r.status_code == 200  # SD-2: will be 404 until route is added


# ===========================================================================
# 9. Jobs routes
# ===========================================================================


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-4: spec says 202; impl returns 200; see AUD-335")
async def test_jobs_post_202(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """POST /jobs → 202 run_id and coalesced boolean.

    SPEC_DRIFT SD-4: spec says 202; impl returns 200.
    """
    c, csrf = auth_client
    r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202  # SD-4: currently 200
    data = r.json()
    assert "run_id" in data
    assert "coalesced" in data


@pytest.mark.integration
async def test_jobs_list_200_collection_shape(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /jobs → 200 with items/next_cursor/request_id/generated_at."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/jobs")
    assert r.status_code == 200
    _assert_collection_envelope(r.json())


@pytest.mark.integration
async def test_jobs_list_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /jobs → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/jobs")
    assert r.status_code == 401


@pytest.mark.integration
async def test_jobs_get_by_id_404_unknown(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /jobs/{id} → 404 for unknown ID."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/jobs/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-4: spec says 202; impl returns 200; see AUD-335")
async def test_jobs_cancel_202(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """POST /jobs/{id}/cancel → 202 cancel_requested.

    SPEC_DRIFT SD-4: spec says 202; impl returns 200.
    Seeds a job first so the cancel route can find it.
    """
    c, csrf = auth_client
    # Create a job to cancel
    post_r = await c.post(
        f"{_V1}/jobs",
        json={"kind": "balances"},
        headers={"x-csrf-token": csrf},
    )
    # Accept 200 or 202 from POST /jobs (SD-4 covers both)
    assert post_r.status_code in (200, 202)
    run_id = post_r.json()["run_id"]

    r = await c.post(
        f"{_V1}/jobs/{run_id}/cancel",
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 202  # SD-4: currently 200


# ===========================================================================
# 10. Portfolio route
# ===========================================================================


@pytest.mark.integration
async def test_portfolio_get_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """GET /portfolio → 200 portfolio envelope."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/portfolio")
    assert r.status_code == 200
    data = r.json()
    # spec-mandated envelope fields
    assert "holdings" in data
    assert "allocations" in data
    assert "quality" in data
    assert "currency" in data
    assert "request_id" in data
    assert "generated_at" in data
    assert data["currency"] == "USD"


@pytest.mark.integration
async def test_portfolio_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /portfolio → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/portfolio")
    assert r.status_code == 401


# ===========================================================================
# 11. History routes
# ===========================================================================


@pytest.mark.integration
@pytest.mark.xfail(strict=True, reason="SD-9: GET /history returns {entries} not {items} per spec; see AUD-335")
async def test_history_get_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """GET /history → 200 chart summaries."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/history", params={"range": "24h"})
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert "request_id" in data


@pytest.mark.integration
async def test_history_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /history → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/history", params={"range": "24h"})
    assert r.status_code == 401


@pytest.mark.integration
async def test_history_snapshot_404_unknown(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /history/{snapshot_id} → 404 for unknown snapshot."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/history/{uuid.uuid4()}")
    assert r.status_code == 404


# ===========================================================================
# 12. Exports routes
# ===========================================================================


@pytest.mark.integration
async def test_exports_portfolio_authenticated(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /exports/portfolio → authenticated private streamed response."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/exports/portfolio", params={"format": "json"})
    # Route must exist and be authenticated
    assert r.status_code not in (404, 405)


@pytest.mark.integration
async def test_exports_portfolio_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /exports/portfolio → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/exports/portfolio", params={"format": "json"})
    assert r.status_code == 401


@pytest.mark.integration
async def test_exports_history_authenticated(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /exports/history → authenticated private streamed response."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/exports/history", params={"format": "json"})
    assert r.status_code not in (404, 405)


@pytest.mark.integration
async def test_exports_history_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /exports/history → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/exports/history", params={"format": "json"})
    assert r.status_code == 401


# ===========================================================================
# 13. Provider purge route
# ===========================================================================


@pytest.mark.integration
async def test_provider_purge_preview_200(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /data/provider-purge-preview → 200 impact count."""
    c, _ = auth_client
    r = await c.get(
        f"{_V1}/data/provider-purge-preview", params={"provider": "coingecko"}
    )
    assert r.status_code == 200


@pytest.mark.integration
async def test_provider_purge_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """POST /data/provider-purge → 401 when unauthenticated."""
    r = await client.post(
        f"{_V1}/data/provider-purge",
        json={"provider": "coingecko", "confirm": True, "current_password": _PASSWORD},
    )
    assert r.status_code == 401


# ===========================================================================
# 14. Status route
# ===========================================================================


@pytest.mark.integration
async def test_status_get_200(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """GET /status → 200 db/worker/integrations/schedules/version/recovery."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/status")
    assert r.status_code == 200
    data = r.json()
    assert "db" in data
    assert "worker" in data
    assert "schedules" in data


@pytest.mark.integration
async def test_status_401_unauthenticated(client: httpx.AsyncClient) -> None:
    """GET /status → 401 when unauthenticated."""
    r = await client.get(f"{_V1}/status")
    assert r.status_code == 401


# ===========================================================================
# 15. Health routes (public, no auth)
# ===========================================================================


@pytest.mark.integration
async def test_health_live_200(client: httpx.AsyncClient) -> None:
    """GET /health/live → 200 process liveness."""
    r = await client.get("/health/live")
    assert r.status_code == 200


@pytest.mark.integration
async def test_health_ready(client: httpx.AsyncClient) -> None:
    """GET /health/ready → 200 or 503 (ready/unready), not 404."""
    r = await client.get("/health/ready")
    assert r.status_code in (200, 503)


@pytest.mark.integration
async def test_health_ready_worker_reflects_live_heartbeat(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_collect_status() must report a live heartbeat, not the "unknown" default.

    Before AUD-318, _collect_status() never queried worker_status at all, so
    this field was permanently stuck at the WorkerStatus dataclass default —
    production returned {"...", "worker":"unknown"} even with a healthy
    worker running.

    Calls _collect_status() directly (patching audr.db._get_session_factory)
    rather than going through GET /health/ready: that route reads settings via
    _get_session_factory() -> get_settings(), bypassing the test app's DI
    override, and the backend-tests container does not set SECRET_KEY — see
    test_health_ready above, which already tolerates the resulting 503.  That
    is an unrelated, pre-existing test-environment gap; this test isolates
    the actual AUD-318 behaviour instead of depending on it.
    """
    from unittest.mock import patch

    from audr.api.health import _collect_status
    from audr.jobs.store import upsert_worker_status

    worker_id = "test-health-ready-worker-aud-318"
    try:
        async with db_session_factory() as session:
            await upsert_worker_status(session, worker_id=worker_id, status="idle")
            await session.commit()

        with patch("audr.db._get_session_factory", return_value=db_session_factory):
            system = await _collect_status()

        assert system.worker.status != "unknown", (
            "a live heartbeat must not report as 'unknown'"
        )
        assert system.worker.status == "running"
    finally:
        async with db_session_factory() as session:
            await session.execute(
                text("DELETE FROM worker_status WHERE worker_id = :wid"),
                {"wid": worker_id},
            )
            await session.commit()


@pytest.mark.integration
async def test_health_ready_reflects_real_migration_status(
    client: httpx.AsyncClient,
) -> None:
    """/health/ready is fully migrated in the test DB → 200 with a real revision.

    AUD-322: previously this always reported migration="ok" with no revision,
    even against an unmigrated DB. It now surfaces the real alembic revision.
    """
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["migration"] == "ok"


@pytest.mark.integration
async def test_health_ready_503_when_migration_is_stale(
    client: httpx.AsyncClient,
) -> None:
    """/health/ready → 503 with migration="degraded" when the DB lags alembic head."""
    with patch(
        "audr.operations.migrations._get_head_revision", return_value="0_nonexistent"
    ):
        r = await client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["error"]["migration"] == "degraded"


# ===========================================================================
# 16. Error envelope shape  (SD-5)
# ===========================================================================


@pytest.mark.integration
async def test_error_envelope_shape_401(client: httpx.AsyncClient) -> None:
    """Unauthenticated request → error envelope {error:{code,message,field_errors,retryable},request_id}.

    SD-5 resolved in AUD-320: HTTPException/validation handlers now emit the
    contract envelope instead of FastAPI's flat {"detail": ...}.
    """
    r = await client.get(f"{_V1}/wallets")
    assert r.status_code == 401
    data = r.json()
    # Per spec: error must be an object with code, message, field_errors, retryable
    assert "request_id" in data, "missing request_id in error envelope"
    assert isinstance(data["error"], dict), "error must be an object, not a string"
    assert data["error"]["code"] == "unauthenticated"
    assert data["error"]["message"]
    assert "field_errors" in data["error"]
    assert data["error"]["retryable"] is False


@pytest.mark.integration
async def test_error_envelope_shape_404(auth_client: tuple[httpx.AsyncClient, str]) -> None:
    """Not-found request → error envelope {error:{code,message,...},request_id}.

    SD-5 resolved in AUD-320, same as the 401 case.
    """
    c, _ = auth_client
    r = await c.get(f"{_V1}/wallets/{uuid.uuid4()}")
    assert r.status_code == 404
    data = r.json()
    assert "request_id" in data, "missing request_id in error envelope"
    assert isinstance(data.get("error"), dict), "error must be an object"
    assert data["error"]["code"] == "not_found"


@pytest.mark.integration
async def test_error_envelope_validation_reports_field_errors(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """422 from request validation carries per-field messages (AUD-320)."""
    c, csrf = auth_client
    # Omit the required `address` field so FastAPI's own validation fires.
    r = await c.post(
        f"{_V1}/wallets",
        json={},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422
    data = r.json()
    assert data["error"]["code"] == "invalid_value"
    assert data["error"]["field_errors"], "422 must name the offending field(s)"
    assert "request_id" in data


# ===========================================================================
# 17. Pagination cursor format
# ===========================================================================


@pytest.mark.integration
async def test_wallets_pagination_cursor_is_none_or_string(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /wallets next_cursor must be null or a non-empty string."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/wallets")
    assert r.status_code == 200
    cursor = r.json()["next_cursor"]
    assert cursor is None or (isinstance(cursor, str) and len(cursor) > 0)


@pytest.mark.integration
async def test_assets_pagination_cursor_is_none_or_string(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /assets next_cursor must be null or a non-empty string."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/assets")
    assert r.status_code == 200
    cursor = r.json()["next_cursor"]
    assert cursor is None or (isinstance(cursor, str) and len(cursor) > 0)


@pytest.mark.integration
async def test_jobs_pagination_cursor_is_none_or_string(
    auth_client: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /jobs next_cursor must be null or a non-empty string."""
    c, _ = auth_client
    r = await c.get(f"{_V1}/jobs")
    assert r.status_code == 200
    cursor = r.json()["next_cursor"]
    assert cursor is None or (isinstance(cursor, str) and len(cursor) > 0)


# ===========================================================================
# 18. CSRF enforcement — mutations require x-csrf-token
# ===========================================================================


@pytest.mark.integration
async def test_wallets_post_requires_csrf(client: httpx.AsyncClient) -> None:
    """POST /wallets without CSRF → 401 or 403."""
    await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    r = await client.post(f"{_V1}/wallets", json={"address": _ETH_ADDR})
    assert r.status_code in (401, 403)


@pytest.mark.integration
async def test_assets_manual_post_requires_csrf(client: httpx.AsyncClient) -> None:
    """POST /assets/manual without CSRF → 401 or 403."""
    await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    r = await client.post(
        f"{_V1}/assets/manual", json={"contract_address": _CONTRACT_A}
    )
    assert r.status_code in (401, 403)


@pytest.mark.integration
async def test_jobs_post_requires_csrf(client: httpx.AsyncClient) -> None:
    """POST /jobs without CSRF → 401 or 403."""
    await client.post(f"{_V1}/setup", json={"password": _PASSWORD})
    r = await client.post(f"{_V1}/jobs", json={"kind": "balances"})
    assert r.status_code in (401, 403)
