"""The OpenAPI schema must never contain a duplicate operationId (AUD-710).

`PUT /auth/password` and `PATCH /auth/password` used to share a single
`APIRoute` built via one `@router.api_route(methods=["PUT", "PATCH"])` call.
FastAPI stores exactly one `operation_id` per `APIRoute`, so both methods
always collapsed onto the same generated id — and *which* method's name won
depended on Python's set iteration order for `route.methods`, which varies
with `PYTHONHASHSEED` across interpreter runs. That produced a "Duplicate
Operation ID" warning on every schema build and, downstream, two
identically-named `operations[...]` members in openapi-typescript's
generated TypeScript (a `TS2300` duplicate-identifier error).

The fix splits the single multi-method route into two routes — one
`@router.put(...)`, one `@router.patch(...)` — each with its own explicit
`operation_id`, so both methods get distinct, stable ids regardless of hash
seed. These tests pin that fix independently of `PYTHONHASHSEED` and of the
specific route it was found on.
"""

from __future__ import annotations

import warnings

import pytest

from audr.api.app import app

pytestmark = pytest.mark.unit


def _build_schema() -> dict:
    # FastAPI caches `app.openapi_schema` after the first call; clear it so
    # every test observes a fresh build (and any warnings it emits).
    app.openapi_schema = None
    return app.openapi()


def test_full_openapi_schema_has_no_duplicate_operation_ids() -> None:
    schema = _build_schema()
    ids = [
        operation["operationId"]
        for item in schema["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(ids) == len(set(ids)), "duplicate operationId(s) in the OpenAPI schema"


def test_building_the_schema_emits_no_duplicate_operation_id_warning() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_schema()
    duplicate_warnings = [w for w in caught if "Duplicate Operation ID" in str(w.message)]
    assert not duplicate_warnings, duplicate_warnings


def test_auth_password_put_and_patch_have_distinct_explicit_operation_ids() -> None:
    schema = _build_schema()
    operations = schema["paths"]["/api/v1/auth/password"]
    assert operations["put"]["operationId"] == "change_password"
    assert operations["patch"]["operationId"] == "change_password_patch"
    assert operations["put"]["operationId"] != operations["patch"]["operationId"]
