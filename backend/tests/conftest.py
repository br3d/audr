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

from tests.fixtures.quotes import CoinGeckoStub, quotes_mock  # noqa: F401 — re-exported fixture
from tests.fixtures.rpc import EthRpcStub, rpc_mock, rpc_url  # noqa: F401 — re-exported fixture
from tests.fixtures.seed import (  # noqa: F401 — re-exported fixtures
    AAVE_ADDRESS,
    BUTERIN_ADDRESS,
    BUTERIN_LABEL,
    DAI_ADDRESS,
    LINK_ADDRESS,
    SEED_ASSETS,
    TEST_PASSWORD,
    UNI_ADDRESS,
    USDC_ADDRESS,
    USDT_ADDRESS,
    WETH_ADDRESS,
    buterin_wallet,
    seeded_assets,
    seeded_client,
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# PostgreSQL test database (integration / contract tests)
# ---------------------------------------------------------------------------

_TEST_DB_URL_ENV = "TEST_DATABASE_URL"
_DEFAULT_TEST_DB_URL = "postgresql+psycopg://audr:audr@localhost:5433/audr_test"


def _test_db_url() -> str:
    # TEST_DATABASE_URL takes precedence; fall back to DATABASE_URL (set in CI compose),
    # then the local dev default.
    return (
        os.environ.get(_TEST_DB_URL_ENV) or os.environ.get("DATABASE_URL") or _DEFAULT_TEST_DB_URL
    )


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


# ---------------------------------------------------------------------------
# AUD-688: compose.test.yaml selects tests by `-m "unit or integration or
# contract"`. That's an allowlist — a test file with no suite marker (or a
# typo'd one) is silently deselected rather than failing, so it never runs in
# CI. pytest_itemcollected fires for every collected item before -m
# deselection happens, so the check below sees tests the selector would have
# dropped.
# ---------------------------------------------------------------------------

_SUITE_MARKERS = ("unit", "integration", "contract")


def pytest_configure(config: pytest.Config) -> None:
    config._suite_marker_violations = []  # type: ignore[attr-defined]


def pytest_itemcollected(item: pytest.Item) -> None:
    matched = [name for name in _SUITE_MARKERS if item.get_closest_marker(name)]
    if len(matched) != 1:
        item.config._suite_marker_violations.append((item.nodeid, matched))  # type: ignore[attr-defined]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    violations: list[tuple[str, list[str]]] = config._suite_marker_violations  # type: ignore[attr-defined]
    if violations:
        details = "\n".join(f"  {nodeid} -> {matched or 'none'}" for nodeid, matched in violations)
        raise pytest.UsageError(
            "Every collected test must carry exactly one suite marker "
            f"({', '.join(_SUITE_MARKERS)}); the CI selector treats unmarked tests as an "
            f"allowlist and silently drops them instead of failing collection:\n{details}"
        )
