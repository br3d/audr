"""Integration test fixtures.

The shared db_session and test_secret_key fixtures are defined in
tests/conftest.py and are available to all integration tests automatically.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.operations.crypto import InvalidEnvelopeError
from audr.operations.init_key import init_key
from tests.conftest import _TEST_SECRET_KEY


@pytest.fixture(autouse=True)
async def _master_key_ready(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """Guarantee every integration test has a usable master key.

    In production the `migrate` container's `init_key` step validates
    SECRET_KEY can unwrap `key_state` before `api`/`worker` ever start
    (docs/operations.md#key-loss-behavior), so by the time a request handler
    runs, wallet-label and credential encryption always have a key. The
    integration suite talks to the app directly over ASGI and skips that
    step, so without this, every wallet create/update (AUD-488) would raise
    MissingKeyError. Wrapped in try/except because a handful of tests in this
    directory commit a deliberately-undecryptable key_state row for their own
    purposes (e.g. test_migrations.py) and leave it in place — this resets it
    rather than letting that poison every later test in the run.
    """
    monkeypatch.setenv("SECRET_KEY", _TEST_SECRET_KEY)
    async with db_session_factory() as session:
        async with session.begin():
            try:
                await init_key(session)
            except InvalidEnvelopeError:
                await session.execute(text("DELETE FROM key_state"))
                await init_key(session)
    yield
