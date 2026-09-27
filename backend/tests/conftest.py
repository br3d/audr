"""Shared pytest fixtures and configuration for all test suites (T003, T015)."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tests.fixtures.quotes import CoinGeckoStub, quotes_mock  # noqa: F401
from tests.fixtures.rpc import EthRpcStub, rpc_mock, rpc_url  # noqa: F401


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# PostgreSQL test database (integration / contract tests)
# ---------------------------------------------------------------------------

_TEST_DB_URL_ENV = "TEST_DATABASE_URL"
_DEFAULT_TEST_DB_URL = "postgresql+psycopg://audr:audr@localhost:5433/audr_test"


def _test_db_url() -> str:
    return os.environ.get(_TEST_DB_URL_ENV, _DEFAULT_TEST_DB_URL)


@pytest.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine]:
    """Session-scoped async engine connected to the test PostgreSQL database."""
    engine = create_async_engine(_test_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def db_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture()
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """Function-scoped database session wrapped in a rolled-back transaction."""
    async with db_session_factory() as session:
        async with session.begin():
            yield session
            await session.rollback()


# ---------------------------------------------------------------------------
# Fake clock (integration / unit tests that need deterministic time)
# ---------------------------------------------------------------------------


class FakeClock:
    """Controllable wall clock for tests."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._now = initial or datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: Any) -> None:
        from datetime import timedelta

        self._now += timedelta(**kwargs)


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# Test SECRET_KEY for encryption tests
# ---------------------------------------------------------------------------

_TEST_SECRET_KEY = "a" * 64  # 32-byte all-0xaa key — safe for tests only


@pytest.fixture(autouse=False)
def test_secret_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("SECRET_KEY", _TEST_SECRET_KEY)
    return _TEST_SECRET_KEY
