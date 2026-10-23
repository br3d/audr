"""export_openapi_json() must describe the full API, not the curated subset
(AUD-694).

The frontend's generated types need every router's shape — including the
session-only operator surfaces `audr.api.openapi_curated` deliberately
drops from the public `/openapi.json` (AUD-633) — so this module calls
`app.openapi()` directly rather than going through the curator.
"""

from __future__ import annotations

import json

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
