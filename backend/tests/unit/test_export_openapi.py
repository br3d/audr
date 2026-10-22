"""export_openapi_json() must describe the full API, not the curated subset
(AUD-694).

The frontend's generated types need every router's shape — including the
session-only operator surfaces `audr.api.openapi_curated` deliberately
drops from the public `/openapi.json` (AUD-633) — so this module calls
`app.openapi()` directly rather than going through the curator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from audr.operations.export_openapi import export_openapi_json

pytestmark = pytest.mark.unit


def test_exports_valid_json_document() -> None:
    document = json.loads(export_openapi_json())
    assert document["openapi"].startswith("3.")
    assert document["paths"]


def test_includes_operator_only_routes_the_curated_schema_drops() -> None:
    document = json.loads(export_openapi_json())
    assert any(path.startswith("/api/v1/settings") for path in document["paths"])
    assert any(path.startswith("/api/v1/integrations") for path in document["paths"])
    assert any(path.startswith("/api/v1/jobs") for path in document["paths"])


def test_includes_non_get_operations() -> None:
    document = json.loads(export_openapi_json())
    methods = {method for path_item in document["paths"].values() for method in path_item}
    assert {"post", "get"}.issubset(methods)


def test_operation_ids_are_unique() -> None:
    """A duplicate operationId collapses to one TypeScript type for two routes
    in the frontend's generated schema — openapi-typescript keys `operations`
    by operationId, so the second declaration silently wins."""
    document = json.loads(export_openapi_json())
    operation_ids = [
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    duplicates = {op_id for op_id in operation_ids if operation_ids.count(op_id) > 1}
    assert not duplicates


def test_committed_schema_matches_the_live_app() -> None:
    """backend/openapi.json is the frontend codegen's input (AUD-694) —
    committed here as a static file so CI need not boot the app to diff the
    generated frontend/src/api/schema.gen.ts against it. Drift here means
    schema.gen.ts was generated from a stale schema."""
    committed_path = Path(__file__).resolve().parents[2] / "openapi.json"
    committed = json.loads(committed_path.read_text())
    assert committed == json.loads(export_openapi_json())
