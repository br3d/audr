"""Keyset pagination for GET /api/v1/jobs (AUD-322).

Previously the `cursor` query param was accepted but silently ignored, and
`next_cursor` was always null — a permanent newest-20 window over an
unbounded job_run table. These tests pin down real pagination behaviour.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

pytestmark = pytest.mark.integration

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_JOBS_URL = "/api/v1/jobs"
_PASSWORD = "correct-horse-battery-staple-99"


@pytest.fixture(autouse=True)
async def _clean_tables(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM job_run"))
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


async def _insert_job(
    session: AsyncSession, *, created_at: datetime, kind: str = "balance_scan"
) -> uuid.UUID:
    job_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO job_run (id, kind, status, created_at)"
            " VALUES (:id, :kind, 'completed', :created_at)"
        ),
        {"id": str(job_id), "kind": kind, "created_at": created_at},
    )
    return job_id


@pytest.mark.integration
async def test_list_jobs_paginates_with_cursor(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Newest-first, three pages of one item each, cursor advances every time."""
    csrf = await _setup_and_get_csrf(http_client)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with db_session_factory() as session:
        async with session.begin():
            job_ids = [
                await _insert_job(session, created_at=base + timedelta(seconds=i)) for i in range(3)
            ]
    newest_first = list(reversed(job_ids))

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        r = await http_client.get(_JOBS_URL, params=params, headers={"x-csrf-token": csrf})
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) == 1
        seen.append(body["items"][0]["id"])
        cursor = body["next_cursor"]

    assert seen == [str(j) for j in newest_first]
    assert cursor is None, "cursor must be exhausted after the last page"


@pytest.mark.integration
async def test_list_jobs_invalid_cursor_returns_400(
    http_client: httpx.AsyncClient,
) -> None:
    csrf = await _setup_and_get_csrf(http_client)
    r = await http_client.get(
        _JOBS_URL,
        params={"cursor": "not-a-uuid"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 400


@pytest.mark.integration
async def test_list_jobs_cursor_composes_with_kind_filter(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Cursor pagination still respects the `kind` filter across pages."""
    csrf = await _setup_and_get_csrf(http_client)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with db_session_factory() as session:
        async with session.begin():
            await _insert_job(session, created_at=base, kind="quote_refresh")
            rpc_ids = [
                await _insert_job(session, created_at=base + timedelta(seconds=i + 1), kind="rpc")
                for i in range(2)
            ]

    r1 = await http_client.get(
        _JOBS_URL,
        params={"kind": "rpc", "limit": 1},
        headers={"x-csrf-token": csrf},
    )
    assert r1.status_code == 200
    page1 = r1.json()
    assert page1["items"][0]["id"] == str(rpc_ids[1])
    assert page1["next_cursor"] is not None

    r2 = await http_client.get(
        _JOBS_URL,
        params={"kind": "rpc", "limit": 1, "cursor": page1["next_cursor"]},
        headers={"x-csrf-token": csrf},
    )
    assert r2.status_code == 200
    page2 = r2.json()
    assert page2["items"][0]["id"] == str(rpc_ids[0])
    assert page2["next_cursor"] is None
