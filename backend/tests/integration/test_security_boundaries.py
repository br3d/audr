"""Security boundary audit (AUD-109 / AUD-373).

Five boundaries from the audit scope, one section each:

1. No leakage of provider keys/credentials in API responses or logs.
2. No wallet-signing capability exists anywhere in the API or RPC client —
   this app only ever reads chain state, never signs or broadcasts.
3. No provider URL leakage in error responses (a configured RPC URL can
   itself embed a secret, e.g. https://mainnet.infura.io/v3/<key>).
4. CSRF cannot be bypassed on state-mutating routes — extends
   test_settings_csrf.py's coverage to the integrations endpoints.
5. Telemetry is disabled and unconfigured by default.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.config import Settings
from audr.db import get_db
from audr.operations.init_key import init_key
from audr.providers.coingecko_demo import CoinGeckoError, CoinGeckoProvider
from audr.providers.rpc_reader import RpcError, RpcReader

pytestmark = pytest.mark.integration

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_INTEGRATIONS_URL = "/api/v1/integrations"
_PASSWORD = "correct-horse-battery-staple-99"

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "audr"


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


def _iter_source_files() -> list[Path]:
    return list(_SRC_ROOT.rglob("*.py"))


# ---------------------------------------------------------------------------
# 1. Key / credential leakage
# ---------------------------------------------------------------------------

_SECRET_API_KEY = "cg-super-secret-demo-key-0001"


async def test_quotes_api_key_never_returned_in_response_body(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/quotes",
        json={"revision": "0", "provider": "coingecko", "api_key": _SECRET_API_KEY},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    assert _SECRET_API_KEY not in r.text
    assert "api_key" not in r.json()

    r2 = await http_client.get(_INTEGRATIONS_URL)
    assert r2.status_code == 200
    assert _SECRET_API_KEY not in r2.text


async def test_rpc_secret_path_segment_never_returned_in_response_body(
    http_client: httpx.AsyncClient,
) -> None:
    """A configured RPC URL may itself embed a provider API key, e.g.
    https://mainnet.infura.io/v3/<project-id>. Only the bare hostname may
    ever be echoed back — never the full URL or its path."""
    csrf = await _setup_and_get_csrf(http_client)
    secret_url = "https://mainnet.infura.io/v3/super-secret-project-id-0001"
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": secret_url},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    assert "super-secret-project-id-0001" not in r.text
    assert r.json()["host_label"] == "mainnet.infura.io"

    r2 = await http_client.get(_INTEGRATIONS_URL)
    assert r2.status_code == 200
    assert "super-secret-project-id-0001" not in r2.text


async def test_failed_validation_job_error_never_leaks_configured_secret(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A validate_rpc job that fails stores str(exc) as job_run.error, which
    GET /integrations surfaces as health.error_message to the owner. Even
    that failure path must never carry the secret-bearing URL through to the
    stored error text (see test_rpc_client_error_never_includes_url below for
    where that guarantee actually lives)."""
    csrf = await _setup_and_get_csrf(http_client)
    secret_url = "https://mainnet.infura.io/v3/super-secret-project-id-0002"
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": secret_url},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    with respx.mock(assert_all_called=False) as mock:
        mock.post(secret_url).mock(return_value=Response(500))
        async with RpcReader(url=secret_url, expected_chain_id=1) as rpc:
            with pytest.raises(RpcError) as excinfo:
                await rpc.validate_chain()
    error_text = str(excinfo.value)
    assert "super-secret-project-id-0002" not in error_text

    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO job_run (id, kind, status, error, completed_at)"
                    " VALUES (:id, 'validate_rpc', 'failed', :error, now())"
                ),
                {"id": uuid.uuid4(), "error": error_text},
            )

    r2 = await http_client.get(_INTEGRATIONS_URL)
    assert r2.status_code == 200
    assert "super-secret-project-id-0002" not in r2.text
    rpc_item = next(i for i in r2.json()["items"] if i["kind"] == "rpc")
    assert rpc_item["health"]["status"] == "error"
    assert rpc_item["health"]["error_message"] is not None


async def test_configure_integrations_never_logs_secret_values(
    http_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    secret_url = "https://mainnet.infura.io/v3/super-secret-project-id-0003"
    with caplog.at_level(logging.DEBUG):
        r1 = await http_client.put(
            f"{_INTEGRATIONS_URL}/rpc",
            json={"revision": "0", "url": secret_url},
            headers={"x-csrf-token": csrf},
        )
        r2 = await http_client.put(
            f"{_INTEGRATIONS_URL}/quotes",
            json={"revision": "0", "provider": "coingecko", "api_key": _SECRET_API_KEY},
            headers={"x-csrf-token": csrf},
        )
        await http_client.get(_INTEGRATIONS_URL)
    assert r1.status_code == 200
    assert r2.status_code == 200

    for record in caplog.records:
        message = record.getMessage()
        assert "super-secret-project-id-0003" not in message
        assert _SECRET_API_KEY not in message


# ---------------------------------------------------------------------------
# 2. No wallet-signing capability
# ---------------------------------------------------------------------------

# Anything beyond read-only chain access — signing, broadcasting, or raw key
# material — must never appear in application source.
_BANNED_SOURCE_PATTERNS = (
    "eth_sign",
    "personal_sign",
    "eth_sendtransaction",
    "eth_sendrawtransaction",
    "signtransaction",
    "signmessage",
    "private_key",
    "privatekey",
    "mnemonic",
    "seed_phrase",
    "seedphrase",
)


def test_no_route_path_exposes_signing() -> None:
    for route in app.routes:
        path = getattr(route, "path", "")
        assert "sign" not in path.lower(), f"route {path!r} looks like a signing endpoint"


def test_no_signing_or_key_material_in_source() -> None:
    offenders: list[str] = []
    for path in _iter_source_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for pattern in _BANNED_SOURCE_PATTERNS:
            if pattern in lowered:
                offenders.append(f"{path.relative_to(_SRC_ROOT)}: {pattern!r}")
    assert offenders == [], f"signing/key-material references found: {offenders}"


def test_wallet_schemas_accept_no_key_material() -> None:
    """AddWalletBody / PatchWalletBody only ever take an address and a label —
    never a private key, mnemonic, or signature."""
    from audr.api.wallets import AddWalletBody, PatchWalletBody

    banned = ("private", "mnemonic", "sign", "secret")
    for model in (AddWalletBody, PatchWalletBody):
        for field_name in model.model_fields:
            lowered = field_name.lower()
            assert not any(b in lowered for b in banned), (
                f"{model.__name__}.{field_name} looks like key material"
            )


def test_rpc_reader_only_exposes_read_methods() -> None:
    """RpcReader's public surface is read-only JSON-RPC calls."""
    public_methods = {
        name
        for name in vars(RpcReader)
        if not name.startswith("_") and callable(getattr(RpcReader, name))
    }
    # Deliberately an exact allowlist, not a "no method named *sign*" heuristic:
    # it fails closed, so adding any new public method to RpcReader forces a
    # conscious decision here about whether that method is read-only.
    allowed = {
        "validate_chain",
        "get_block_number",
        "get_block_time",
        "get_eth_balance",
        "get_erc20_balance",
        "eth_call",
        "get_logs",
    }
    assert public_methods <= allowed, f"unexpected RpcReader methods: {public_methods - allowed}"


# ---------------------------------------------------------------------------
# 3. No provider URL leakage in error responses
# ---------------------------------------------------------------------------


async def test_rpc_client_error_never_includes_url(
    http_client: httpx.AsyncClient,
) -> None:
    """The RPC client's own exception text — what ultimately becomes
    job_run.error and health.error_message — must never embed the endpoint
    URL, since that URL may carry a provider API key in its path."""
    secret_url = "https://mainnet.infura.io/v3/super-secret-project-id-0004"
    with respx.mock(assert_all_called=False) as mock:
        mock.post(secret_url).mock(return_value=Response(500))
        async with RpcReader(url=secret_url, expected_chain_id=1) as rpc:
            with pytest.raises(RpcError) as excinfo:
                await rpc.validate_chain()
    message = str(excinfo.value)
    assert secret_url not in message
    assert "super-secret-project-id-0004" not in message


async def test_quotes_client_error_never_includes_api_key() -> None:
    secret_key = "cg-super-secret-demo-key-0002"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__regex=r".*/simple/price.*").mock(
            return_value=Response(401, text="unauthorized")
        )
        async with CoinGeckoProvider(api_key=secret_key) as provider:
            with pytest.raises(CoinGeckoError) as excinfo:
                await provider.get_eth_price()
    assert secret_key not in str(excinfo.value)


async def test_unhandled_exception_never_echoes_details(
    http_client: httpx.AsyncClient,
) -> None:
    """A generic 500 must only ever carry the sanitized envelope message —
    never the underlying exception text, which could carry a provider URL or
    credential (api/errors.py: unhandled_exception_handler)."""
    await _setup_and_get_csrf(http_client)

    async def _boom() -> AsyncGenerator[AsyncSession]:
        raise RuntimeError("leaking https://mainnet.infura.io/v3/do-not-leak-me")
        yield  # pragma: no cover - unreachable, satisfies generator typing

    # Starlette's ServerErrorMiddleware sends the handler's sanitized response
    # *and* re-raises so the ASGI server still logs the traceback. Under uvicorn
    # the client only ever sees the response; under ASGITransport the re-raise
    # surfaces in-process, so this one client opts out of it to assert on what
    # actually goes over the wire.
    quiet_transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    app.dependency_overrides[get_db] = _boom
    try:
        async with httpx.AsyncClient(transport=quiet_transport, base_url=_BASE) as quiet_client:
            quiet_client.cookies.update(http_client.cookies)
            r = await quiet_client.get(_INTEGRATIONS_URL)
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert r.status_code == 500
    assert "do-not-leak-me" not in r.text
    assert "infura" not in r.text.lower()
    body = r.json()
    assert body["error"]["message"] == "An unexpected error occurred."


async def test_invalid_rpc_url_validation_error_omits_path_and_query(
    http_client: httpx.AsyncClient,
) -> None:
    """A rejected RPC URL (private/loopback host) is echoed back with enough
    context to fix the mistake, but never with query-string or path secrets —
    validate_rpc_url's error only ever names the offending hostname."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": "http://127.0.0.1:8545/v3/some-secret?token=abc123"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422
    assert "some-secret" not in r.text
    assert "abc123" not in r.text


# ---------------------------------------------------------------------------
# 4. CSRF bypass
# ---------------------------------------------------------------------------


async def test_put_integrations_rpc_without_csrf_token_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": "https://mainnet.infura.io/v3/test-key"},
        # No X-CSRF-Token header
    )
    assert r.status_code == 403


async def test_put_integrations_quotes_without_csrf_token_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/quotes",
        json={"revision": "0", "provider": "coingecko", "api_key": "x"},
        # No X-CSRF-Token header
    )
    assert r.status_code == 403


async def test_post_integrations_validate_without_csrf_token_is_rejected(
    http_client: httpx.AsyncClient,
) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.post(
        f"{_INTEGRATIONS_URL}/rpc/validate",
        # No X-CSRF-Token header
    )
    assert r.status_code == 403


async def test_csrf_protected_route_rejects_cross_origin_request(
    http_client: httpx.AsyncClient,
) -> None:
    """_require_csrf also rejects a mismatched Origin header outright, even
    with a valid CSRF token — a belt-and-braces check against CSRF bypass via
    a stolen token replayed from another origin."""
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/rpc",
        json={"revision": "0", "url": "https://mainnet.infura.io/v3/test-key"},
        headers={"x-csrf-token": csrf, "origin": "https://evil.example.com"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 5. Telemetry disabled / unconfigured by default
# ---------------------------------------------------------------------------

_TELEMETRY_SDK_MARKERS = (
    "sentry",
    "posthog",
    "mixpanel",
    "segment.",
    "amplitude",
    "datadog",
    "ddtrace",
    "google_analytics",
    "googleanalytics",
    "plausible",
    "umami",
    "rudderstack",
)


def test_settings_model_has_no_telemetry_fields() -> None:
    for field_name in Settings.model_fields:
        lowered = field_name.lower()
        assert "telemetry" not in lowered
        assert "analytics" not in lowered


def test_no_telemetry_sdk_in_dependencies() -> None:
    pyproject = (_SRC_ROOT.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    lowered = pyproject.lower()
    for marker in _TELEMETRY_SDK_MARKERS:
        assert marker not in lowered, f"telemetry dependency marker found: {marker!r}"


def test_no_telemetry_sdk_imported_in_source() -> None:
    offenders: list[str] = []
    for path in _iter_source_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for marker in _TELEMETRY_SDK_MARKERS:
            if marker in lowered:
                offenders.append(f"{path.relative_to(_SRC_ROOT)}: {marker!r}")
    assert offenders == [], f"telemetry SDK references found: {offenders}"


async def test_fresh_install_status_reports_no_telemetry_opt_in(
    http_client: httpx.AsyncClient,
) -> None:
    """GET /status (the one endpoint that reports app-wide operational state)
    carries no telemetry/analytics flag at all — there is nothing to turn on
    by default because the capability does not exist."""
    await _setup_and_get_csrf(http_client)
    r = await http_client.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()

    def _walk(value: object) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                assert "telemetry" not in key.lower()
                assert "analytics" not in key.lower()
                _walk(inner)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(body)
