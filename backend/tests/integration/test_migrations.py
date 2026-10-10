"""Integration tests for migration readiness and transactional upgrade handling (T088 / US4)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.operations.migrations import _get_head_revision, check_migration_readiness
from audr.operations.reset_password import ResetPasswordError, reset_password

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OWNER_PASSWORD = "migration-test-password-abc"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _reseed_owner_and_key_state(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """Truncate and reseed owner + key_state before each test.

    Uses committed sessions so the rows are visible to subsequent transactions
    (including the rollback-wrapped db_session used in each test).
    """
    from audr.auth.service import setup_owner

    # Clean up all rows that might carry over from previous tests.
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM owner"))
            await session.execute(text("DELETE FROM key_state"))

    # Seed owner (setup_owner commits internally).
    async with db_session_factory() as session:
        await setup_owner(session, _OWNER_PASSWORD)

    # Seed a dummy key_state row so reset_password tests can assert it survived.
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO key_state (name, wrapped_key)"
                    " VALUES ('master_key', :blob)"
                    " ON CONFLICT (name) DO NOTHING"
                ),
                {"blob": b"\xab" * 64},
            )

    yield


# ---------------------------------------------------------------------------
# Tests: check_migration_readiness
# ---------------------------------------------------------------------------


async def test_check_migration_readiness_at_head(db_session: AsyncSession) -> None:
    """check_migration_readiness against a fully-migrated DB returns up_to_date=True."""
    result = await check_migration_readiness(db_session)

    assert result["up_to_date"] is True
    # Revision-agnostic: pinning a literal revision id makes this test fail on
    # every new migration (it broke when 0002_drop_cron_expr landed). What the
    # test actually cares about is that current has caught up to head.
    assert result["current"] == result["head"]


async def test_check_migration_readiness_stale(db_session: AsyncSession) -> None:
    """Patching _get_head_revision to a future revision reports up_to_date=False."""
    with patch("audr.operations.migrations._get_head_revision", return_value="999"):
        result = await check_migration_readiness(db_session)

    assert result["up_to_date"] is False
    assert result["head"] == "999"


async def test_migration_readiness_current_matches_alembic_version(
    db_session: AsyncSession,
) -> None:
    """The 'current' field must match the raw alembic_version table value."""
    raw = await db_session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
    raw_row = raw.first()
    raw_version: str | None = raw_row[0] if raw_row is not None else None

    result = await check_migration_readiness(db_session)

    assert result["current"] == raw_version


async def test_no_alembic_version_returns_unknown(db_session: AsyncSession) -> None:
    """When alembic_version is empty, current=None and up_to_date=False."""
    # Delete within the rollback-wrapped session — restored automatically after the test.
    await db_session.execute(text("DELETE FROM alembic_version"))

    result = await check_migration_readiness(db_session)

    assert result["current"] is None
    assert result["up_to_date"] is False


# ---------------------------------------------------------------------------
# Tests: server defaults (AUD-321)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("quote_set", "status", "'pending'::text"),
        ("wallet", "status", "'active'::text"),
    ],
)
async def test_string_defaults_are_not_double_quoted(
    db_session: AsyncSession, table: str, column: str, expected: str
) -> None:
    """The 0001 baseline double-escaped these defaults, so they stored the quote
    characters and violated their own CHECK constraints. 0007 repairs them.

    wallet.label is excluded here (AUD-488 replaced it with label_ciphertext,
    which has no server default — every row must supply real ciphertext)."""
    row = await db_session.execute(
        text(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    assert row.scalar() == expected


async def test_defaulted_insert_satisfies_check_constraints(
    db_session: AsyncSession,
) -> None:
    """Inserting while relying on the server defaults must not trip a CHECK.

    wallet.label_ciphertext has no default (AUD-488) and is supplied
    explicitly here; the default under test for wallet is status only.
    """
    await db_session.execute(text("INSERT INTO quote_set (provider) VALUES ('coingecko')"))
    await db_session.execute(
        text(
            "INSERT INTO wallet (address, label_ciphertext)"
            " VALUES ('0x000000000000000000000000000000000000dead', '')"
        )
    )

    quote_status = await db_session.execute(
        text("SELECT status FROM quote_set ORDER BY created_at DESC LIMIT 1")
    )
    assert quote_status.scalar() == "pending"

    wallet_row = await db_session.execute(
        text(
            "SELECT status FROM wallet WHERE address = '0x000000000000000000000000000000000000dead'"
        )
    )
    assert wallet_row.scalar() == "active"


# ---------------------------------------------------------------------------
# Tests: schedule interval defaults (AUD-366)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected_freshness_s"),
    [
        ("discovery", 86400),
        ("quote_refresh", 3600),
    ],
)
async def test_schedule_intervals_raised_to_cut_rpc_quota_burn(
    db_session: AsyncSession, kind: str, expected_freshness_s: int
) -> None:
    """0013 raises discovery to 24h and quote_refresh to 1h (AUD-366) so a
    single configured RPC/quote provider's quota is not burned as fast."""
    row = await db_session.execute(
        text("SELECT freshness_s FROM schedule WHERE kind = :kind"),
        {"kind": kind},
    )
    assert row.scalar() == expected_freshness_s


# ---------------------------------------------------------------------------
# Tests: reset_password
# ---------------------------------------------------------------------------


async def test_reset_password_revokes_sessions_not_key(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """reset_password commits, revokes sessions, and leaves key_state intact."""
    async with db_session_factory() as session:
        result = await reset_password(session, new_password="new-valid-password-xyz")

    assert result["password_changed"] is True
    assert result["sessions_revoked"] >= 0

    # key_state row must still be present — reset_password must never touch it.
    async with db_session_factory() as verify:
        key_row = await verify.execute(
            text("SELECT COUNT(*) FROM key_state WHERE name = 'master_key'")
        )
        key_count: int = int(key_row.scalar() or 0)
        assert key_count == 1, "key_state must survive a password reset"


async def test_reset_password_too_short_raises(db_session: AsyncSession) -> None:
    """reset_password raises ValueError when the new password is under 12 characters."""
    with pytest.raises(ValueError, match="at least 12 characters"):
        await reset_password(db_session, new_password="short")


async def test_reset_password_no_owner_raises(db_session: AsyncSession) -> None:
    """reset_password raises ResetPasswordError when no owner row exists."""
    # Remove the owner within this rollback-wrapped session.
    await db_session.execute(text("DELETE FROM session"))
    await db_session.execute(text("DELETE FROM owner"))

    with pytest.raises(ResetPasswordError, match="no owner found"):
        await reset_password(db_session, new_password="new-valid-password-xyz")


# ---------------------------------------------------------------------------
# Tests: _get_head_revision edge cases (AUD-277)
# ---------------------------------------------------------------------------


def test_get_head_revision_multiple_heads_raises() -> None:
    """_get_head_revision raises RuntimeError when the migration tree has diverged (>1 head)."""
    mock_script = MagicMock()
    mock_script.get_heads.return_value = ["abc1234", "def5678"]

    with patch("alembic.script.ScriptDirectory.from_config", return_value=mock_script):
        with pytest.raises(RuntimeError, match="multiple alembic heads"):
            _get_head_revision()


def test_get_head_revision_no_heads_returns_none() -> None:
    """_get_head_revision returns None when no migration scripts exist."""
    mock_script = MagicMock()
    mock_script.get_heads.return_value = []

    with patch("alembic.script.ScriptDirectory.from_config", return_value=mock_script):
        result = _get_head_revision()

    assert result is None
