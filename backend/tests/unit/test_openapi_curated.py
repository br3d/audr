"""The curated OpenAPI document must never describe a private route (AUD-633).

`build_curated_openapi()` filters FastAPI's auto-generated schema down to the
GET operations on `audr.api.auth._is_token_allowed_path` — the exact same
predicate `_require_read_auth` enforces on a live request. These tests pin
that behaviour independently of the production allow-list: the forbidden
prefixes below are hardcoded, not imported from `auth.py`, so a future change
that widens the allow-list insecurely still fails here.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from audr.api.app import app
from audr.api.auth import ReadPrincipal, _require_read_auth
from audr.api.openapi_curated import build_curated_openapi

_read_auth_dep = Depends(_require_read_auth)

pytestmark = pytest.mark.unit

# Operator/secret surfaces that must never become token- or LLM-readable
# (docs/llm-access.md §2, §6). Hardcoded independently of
# audr.api.auth._TOKEN_ALLOWED_PREFIXES on purpose.
_FORBIDDEN_PREFIXES = (
    "/api/v1/integrations",
    "/api/v1/settings",
    "/api/v1/jobs",
    "/api/v1/data",
    "/api/v1/exports",
    "/api/v1/tokens",
    "/api/v1/auth",
    "/api/v1/setup",
    "/api/v1/catalog",
    "/api/v1/wallets",  # bare /wallets* — .../holdings is allowed, asserted below
    "/api/v1/networks",
)

_EXPECTED_ALLOWED_PATHS = frozenset(
    {
        "/api/v1/portfolio",
        "/api/v1/portfolio/holdings",
        "/api/v1/wallets/{wallet_id}/holdings",
        "/api/v1/assets",
        "/api/v1/assets/{asset_id}/icon",
        "/api/v1/assets/{asset_id}/news",
        "/api/v1/history",
        "/api/v1/history/{snapshot_id}",
        "/api/v1/events",
        "/api/v1/allowances",
        "/api/v1/alerts",
        "/api/v1/status",
        "/api/v1/version",
        "/api/v1/llm/context",
        "/api/v1/llm/asset/{ref}",
    }
)


@pytest.fixture(scope="module")
def curated() -> dict:
    return build_curated_openapi(app)


def test_curated_schema_has_no_forbidden_prefix(curated: dict) -> None:
    # The one deliberate exception to the bare "/api/v1/wallets" ban: a
    # wallet's own holdings are read-surface, the rest of /wallets is not.
    exempt = "/api/v1/wallets/{wallet_id}/holdings"
    for path in curated["paths"]:
        if path == exempt:
            continue
        for forbidden in _FORBIDDEN_PREFIXES:
            assert not path.startswith(forbidden), (
                f"{path!r} starts with forbidden prefix {forbidden!r} — a private "
                "or operator route leaked into the curated OpenAPI document"
            )


def test_curated_schema_matches_the_expected_allow_listed_paths(curated: dict) -> None:
    assert set(curated["paths"]) == _EXPECTED_ALLOWED_PATHS


def test_curated_schema_only_ever_exposes_get_operations(curated: dict) -> None:
    for path, item in curated["paths"].items():
        assert set(item.keys()) == {"get"}, f"{path!r} exposes a non-GET method: {item.keys()}"


def test_curated_schema_has_no_mutating_paths_at_all(curated: dict) -> None:
    """Every real mutating route (POST/PATCH/DELETE/PUT) is absent outright.

    Regression target: a route whose *path* happens to sit under an allowed
    prefix (e.g. POST /api/v1/alerts/{alert_id}/read) must still be excluded
    because it has no GET method — not kept via a path-level allow-list.
    """
    full = app.openapi()
    mutating_paths_under_allowed_prefixes = {
        path
        for path, item in full["paths"].items()
        if path in curated["paths"] and set(item.keys()) - {"get"}
    }
    for path in mutating_paths_under_allowed_prefixes:
        kept_methods = set(curated["paths"][path].keys())
        assert "post" not in kept_methods
        assert "patch" not in kept_methods
        assert "delete" not in kept_methods
        assert "put" not in kept_methods


def _ref_names(node: object) -> set[str]:
    """Recursively collect every `#/components/schemas/<Name>` reference in *node*."""
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _ref_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _ref_names(item)
    return found


def test_curated_schema_components_only_reference_kept_schemas(curated: dict) -> None:
    """Every $ref inside `paths` resolves inside the curated `components.schemas`."""
    schemas = curated.get("components", {}).get("schemas", {})
    missing = _ref_names(curated["paths"]) - set(schemas)
    assert not missing, f"paths reference schemas missing from curated components: {missing}"


def test_a_newly_added_private_route_does_not_leak_into_the_curated_schema() -> None:
    """Simulates exactly the regression this module exists to prevent.

    A fresh app wired the same way as `create_app()` (the read-auth
    dependency gates every route), with one new GET route added under a
    forbidden prefix. The curator must drop it on its own — nobody has to
    remember to update an allow-list a second time.
    """
    probe_app = FastAPI()

    @probe_app.get("/api/v1/settings/leaked-probe")
    async def _leaked(_auth: ReadPrincipal = _read_auth_dep) -> dict:
        return {"secret": "should never be schema-visible"}

    @probe_app.get("/api/v1/portfolio/extra-probe")
    async def _extra(_auth: ReadPrincipal = _read_auth_dep) -> dict:
        return {"fine": "allowed prefix"}

    curated = build_curated_openapi(probe_app)

    assert "/api/v1/settings/leaked-probe" not in curated["paths"]
    assert "/api/v1/portfolio/extra-probe" in curated["paths"]


def test_curated_openapi_is_served_unauthenticated_at_root() -> None:
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body["paths"]) == _EXPECTED_ALLOWED_PATHS
