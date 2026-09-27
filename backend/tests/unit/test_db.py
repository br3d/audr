"""Unit tests for database session management."""

import inspect

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr import db
from audr.config import Settings


@pytest.mark.unit
def test_get_db_is_async_generator_function() -> None:
    assert inspect.isasyncgenfunction(db.get_db)


@pytest.mark.unit
def test_make_session_factory_returns_async_sessionmaker() -> None:
    engine = db._make_engine("postgresql+psycopg://user:pass@localhost/audr")
    factory = db._make_session_factory(engine)
    assert isinstance(factory, async_sessionmaker)


@pytest.mark.unit
def test_get_session_factory_returns_async_sessionmaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db._get_session_factory.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    # Override get_settings so it reads the monkeypatched env
    monkeypatch.setattr(db, "get_settings", lambda: Settings())
    try:
        factory = db._get_session_factory()
        assert isinstance(factory, async_sessionmaker)
    finally:
        db._get_session_factory.cache_clear()


@pytest.mark.unit
async def test_get_db_yields_async_session(monkeypatch: pytest.MonkeyPatch) -> None:
    db._get_session_factory.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.setattr(db, "get_settings", lambda: Settings())
    try:
        gen = db.get_db()
        session = await gen.__anext__()
        assert isinstance(session, AsyncSession)
        await gen.aclose()
    finally:
        db._get_session_factory.cache_clear()
