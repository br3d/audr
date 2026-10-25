"""Integration tests for the news-analysis gate settings API (AUD-658).

Covers:
  - GET /api/v1/settings/news-analysis: off by default
  - PATCH /api/v1/settings/news-analysis: updates flag/threshold, bumps revision
  - PATCH: 422 on an out-of-range threshold
  - PATCH: 409 on a stale expected_revision
  - PUT /api/v1/integrations/llm: stores the Anthropic key, never echoes it back
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
_NEWS_ANALYSIS_URL = "/api/v1/settings/news-analysis"
_INTEGRATIONS_URL = "/api/v1/integrations"
_PASSWORD = "correct-horse-battery-staple-99"

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> AsyncGenerator[None]:
    await _reset(db_session_factory)
    async with db_session_factory() as session:
        async with session.begin():
            await init_key(session)
    yield
    await _reset(db_session_factory)


async def _reset(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE news_analysis_setting SET enabled = false, importance_threshold = 50")
            )
            await session.execute(text("DELETE FROM integration"))
            await session.execute(text("DELETE FROM key_state"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))


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


async def test_get_news_analysis_off_by_default(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(_NEWS_ANALYSIS_URL)
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert data["importance_threshold"] == 50
    assert data["revision"] == 1


async def test_patch_enables_the_gate_and_bumps_revision(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.patch(
        _NEWS_ANALYSIS_URL,
        json={"enabled": True, "importance_threshold": 70, "expected_revision": 1},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["importance_threshold"] == 70
    assert data["revision"] == 2

    r2 = await http_client.get(_NEWS_ANALYSIS_URL)
    assert r2.json()["enabled"] is True
    assert r2.json()["importance_threshold"] == 70


@pytest.mark.parametrize("threshold", [-1, 101])
async def test_patch_rejects_out_of_range_threshold(
    http_client: httpx.AsyncClient, threshold: int
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.patch(
        _NEWS_ANALYSIS_URL,
        json={"importance_threshold": threshold},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


async def test_patch_stale_revision_returns_409(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.patch(
        _NEWS_ANALYSIS_URL,
        json={"enabled": True, "expected_revision": 99},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 409


async def test_patch_requires_csrf(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.patch(_NEWS_ANALYSIS_URL, json={"enabled": True})
    assert r.status_code == 403


async def test_put_integration_llm_stores_key_without_echoing_it(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/llm",
        json={"revision": "0", "api_key": "sk-ant-super-secret"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["kind"] == "llm"
    assert data["configured"] is True
    assert data["revision"] == "1"
    assert "sk-ant-super-secret" not in r.text


async def test_put_integration_llm_rejects_empty_key(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/llm",
        json={"revision": "0", "api_key": "   "},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 422


async def test_put_integration_llm_revision_conflict_returns_409(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.put(
        f"{_INTEGRATIONS_URL}/llm",
        json={"revision": "0", "api_key": "sk-ant-one"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    r2 = await http_client.put(
        f"{_INTEGRATIONS_URL}/llm",
        json={"revision": "0", "api_key": "sk-ant-two"},
        headers={"x-csrf-token": csrf},
    )
    assert r2.status_code == 409
