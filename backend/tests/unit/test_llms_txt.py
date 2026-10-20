"""GET /.well-known/llms.txt — a public pointer for LLM/automation clients (AUD-633).

AC coverage:
- Served without authentication.
- Contains no portfolio data — it is a static, instance-generic pointer.
- Names how to authenticate (Bearer token) and where the schema lives.
- States plainly that figures can be stale and total_usd can be null.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audr.api.app import app

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def body() -> str:
    with TestClient(app) as client:
        response = client.get("/.well-known/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    return response.text


def test_llms_txt_requires_no_auth(body: str) -> None:
    assert body  # the fixture already asserted the 200 without any auth header


def test_llms_txt_points_at_the_curated_schema(body: str) -> None:
    assert "/openapi.json" in body


def test_llms_txt_explains_bearer_auth(body: str) -> None:
    lower = body.lower()
    assert "authorization" in lower
    assert "bearer" in lower


def test_llms_txt_warns_about_staleness_and_null_total_usd(body: str) -> None:
    lower = body.lower()
    assert "stale" in lower
    assert "total_usd" in lower
    assert "null" in lower


def test_llms_txt_is_not_shadowed_by_the_spa_fallback(body: str) -> None:
    assert '<div id="root"' not in body
