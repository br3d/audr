"""Integration tests for GET /assets/{id}/icon (AUD-385)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_PASSWORD = "correct-horse-battery-staple-99"
_CONTRACT_A = "0x" + "a" * 40
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = ("asset_icon", "asset", "login_attempt", "session", "owner")


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for table in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {table}"))


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


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


async def _insert_asset(
    db_session_factory: async_sessionmaker[AsyncSession], token_address: str
) -> str:
    asset_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                    " VALUES (:id, :addr, 'TKN', 'Token', 18, 'catalog')"
                ),
                {"id": asset_id, "addr": token_address.lower()},
            )
    return asset_id


async def _insert_icon(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_id: str,
    status: str,
    content_type: str | None = None,
    image: bytes | None = None,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset_icon (asset_id, content_type, image, source, status)"
                    " VALUES (:id, :ct, :img, :src, :status)"
                ),
                {
                    "id": asset_id,
                    "ct": content_type,
                    "img": image,
                    "src": "trustwallet" if status == "ok" else None,
                    "status": status,
                },
            )


@pytest.mark.integration
async def test_get_icon_returns_cached_bytes(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A)
    await _insert_icon(
        db_session_factory,
        asset_id=asset_id,
        status="ok",
        content_type="image/png",
        image=_PNG_BYTES,
    )

    r = await http_client.get(f"/api/v1/assets/{asset_id}/icon")
    assert r.status_code == 200
    assert r.content == _PNG_BYTES
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=604800"


@pytest.mark.integration
async def test_get_icon_404_for_unknown_asset(http_client: httpx.AsyncClient) -> None:
    await _setup_and_get_csrf(http_client)
    r = await http_client.get(f"/api/v1/assets/{uuid.uuid4()}/icon")
    assert r.status_code == 404


@pytest.mark.integration
async def test_get_icon_404_for_negative_cached_asset(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_get_csrf(http_client)
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A)
    await _insert_icon(db_session_factory, asset_id=asset_id, status="missing")

    r = await http_client.get(f"/api/v1/assets/{asset_id}/icon")
    assert r.status_code == 404


@pytest.mark.integration
async def test_get_icon_requires_session(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    asset_id = await _insert_asset(db_session_factory, _CONTRACT_A)
    r = await http_client.get(f"/api/v1/assets/{asset_id}/icon")
    assert r.status_code == 401
