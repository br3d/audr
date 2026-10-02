"""The SPA served from FastAPI replaces the nginx `web` container (AUD-388).

These lock in the behaviours the old `nginx/nginx.conf` provided, because
losing any of them is silent: a broken SPA fallback only shows up as a 404 on
a hard refresh, and a too-greedy fallback only shows up as the frontend
rendering an API error as HTML.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audr.api.spa import mount_spa
from audr.config import SpaSettings

pytestmark = pytest.mark.unit

INDEX_BODY = '<!doctype html><div id="root"></div>'


@pytest.fixture
def spa_dir(tmp_path):
    """A stand-in for the Vite build that the runtime image copies to /app/static."""
    (tmp_path / "index.html").write_text(INDEX_BODY)
    (tmp_path / "favicon.ico").write_bytes(b"\x00")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-abc123.js").write_text("console.log(1)")
    return tmp_path


@pytest.fixture
def client(spa_dir):
    """A minimal app with the same router-then-SPA ordering as `create_app`."""
    app = FastAPI()

    @app.get("/api/v1/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    assert mount_spa(app, spa_dir) is True
    return TestClient(app)


def test_root_serves_the_spa(client):
    response = client.get("/")
    assert response.status_code == 200
    assert '<div id="root"' in response.text


@pytest.mark.parametrize("route", ["/folio", "/wallets", "/settings/integrations"])
def test_deep_client_route_serves_index_not_404(client, route):
    """A hard refresh on a client-side route has no file on disk.

    This is the `try_files $uri $uri/ /index.html` behaviour nginx provided.
    """
    response = client.get(route)
    assert response.status_code == 200
    assert '<div id="root"' in response.text


def test_real_files_are_served_verbatim(client):
    assert client.get("/favicon.ico").content == b"\x00"
    assert client.get("/assets/index-abc123.js").text == "console.log(1)"


def test_api_routes_are_not_shadowed(client):
    assert client.get("/api/v1/ping").json() == {"ok": True}
    assert client.get("/health/ready").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api/v1/does-not-exist", "/api", "/health/does-not-exist"])
def test_unknown_reserved_path_404s_instead_of_falling_back_to_the_spa(client, path):
    """The SPA catch-all must not swallow unknown API paths.

    Without the reserved-prefix guard the `Mount("/")` answers these with
    index.html and HTTP 200, so a frontend bug against a mistyped endpoint
    would look like a successful request returning HTML.
    """
    response = client.get(path)
    assert response.status_code == 404
    assert '<div id="root"' not in response.text


def test_openapi_schema_is_not_shadowed_by_the_spa(client):
    """A real route, so it wins the match — but assert it, since `/openapi.json`
    sits outside the `/api` and `/health` prefixes and would otherwise be the
    one reserved path the catch-all could claim."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_unknown_api_path_404s_for_non_get_methods_too(client):
    """`StaticFiles` answers non-GET/HEAD with 405 before the path is inspected."""
    response = client.post("/api/v1/does-not-exist", json={})
    assert response.status_code == 404


def test_missing_hashed_asset_404s_rather_than_returning_html(client):
    """A stale index.html asking for a bundle this build lacks must get a 404.

    Falling back to index.html here would hand HTML to a browser expecting
    JavaScript, which reports as an unrelated-looking parse error.
    """
    response = client.get("/assets/index-staleversion.js")
    assert response.status_code == 404
    assert '<div id="root"' not in response.text


def test_mount_is_skipped_when_the_directory_is_absent(tmp_path):
    """The dev/test case: no build output, so nothing is mounted and / is a 404."""
    app = FastAPI()
    assert mount_spa(app, tmp_path / "nope") is False
    assert TestClient(app).get("/").status_code == 404


def test_mount_is_skipped_when_index_html_is_missing(tmp_path):
    (tmp_path / "assets").mkdir()
    app = FastAPI()
    assert mount_spa(app, tmp_path) is False


def test_real_app_serves_the_spa_without_shadowing_the_api(spa_dir, monkeypatch):
    """End-to-end over the actual `create_app()`, not a stand-in.

    The unit cases above build their own two-route app, so they would still
    pass if `create_app` mounted the SPA *before* including the routers. This
    exercises the real registration order and the real error envelope.
    """
    from audr.api.app import create_app
    from audr.config import get_spa_settings

    monkeypatch.setenv("SERVE_SPA", "true")
    monkeypatch.setenv("SPA_DIR", str(spa_dir))
    # `get_spa_settings` is lru_cached and `audr.api.app` already built a
    # module-level app at import time, so the cache has to be dropped on both
    # sides of this test.
    get_spa_settings.cache_clear()
    try:
        client = TestClient(create_app())

        assert '<div id="root"' in client.get("/").text
        assert '<div id="root"' in client.get("/folio").text

        unknown = client.get("/api/v1/does-not-exist")
        assert unknown.status_code == 404
        assert unknown.headers["content-type"].startswith("application/json")
        assert "root" not in unknown.text

        # Deliberately not asserted here: that `/api/v1/auth/session` still
        # 401s. Reaching a real route means opening a DB session, which needs
        # DATABASE_URL *and* SECRET_KEY, and the test image sets neither at the
        # process level (conftest injects SECRET_KEY per-test via monkeypatch).
        # That path is covered by the integration suite and by
        # scripts/smoke-test.sh. What matters for the SPA mount is that the
        # route is still *matched* by the router rather than swallowed by the
        # catch-all — which the 404-with-JSON assertion above establishes.
    finally:
        get_spa_settings.cache_clear()


def test_spa_settings_default_to_serving_from_the_image_static_dir():
    """`create_app` runs at import time, so these must resolve with no env set."""
    settings = SpaSettings(_env_file=None)
    assert settings.serve_spa is True
    assert settings.spa_dir == "/app/static"


def test_spa_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("SERVE_SPA", "false")
    assert SpaSettings(_env_file=None).serve_spa is False
