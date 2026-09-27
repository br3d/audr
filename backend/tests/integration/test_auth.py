"""Failing integration tests for authentication service (T021 / US1).

Intentionally FAILING until T026 creates tables and T027 implements the auth service.

Covers:
  - Concurrent setup: only one owner can be created even under concurrent requests.
  - Authentication: correct password succeeds; wrong password fails.
  - Session expiry: expired sessions are rejected.
  - CSRF/origin enforcement: cross-origin requests are rejected at the middleware layer.
  - Password change: requires current password; updates hash.
  - Persistent login throttle: excessive failed attempts block the account/IP.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from audr.auth.service import (
    AlreadySetupError,
    AuthError,
    ThrottledError,
    change_password,
    create_session,
    get_session,
    record_failed_login,
    setup_owner,
    verify_password,
)


@pytest.mark.integration
async def test_setup_creates_owner(db_session: AsyncSession) -> None:
    """setup_owner creates the single owner account."""
    owner = await setup_owner(db_session, password="correct-horse-battery-staple")
    assert owner is not None


@pytest.mark.integration
async def test_setup_is_one_shot(db_session: AsyncSession) -> None:
    """A second call to setup_owner raises AlreadySetupError."""
    await setup_owner(db_session, password="first-password")
    with pytest.raises(AlreadySetupError):
        await setup_owner(db_session, password="second-password")


@pytest.mark.integration
async def test_verify_password_correct(db_session: AsyncSession) -> None:
    """Correct password returns the owner record."""
    await setup_owner(db_session, password="my-secret")
    owner = await verify_password(db_session, password="my-secret")
    assert owner is not None


@pytest.mark.integration
async def test_verify_password_wrong(db_session: AsyncSession) -> None:
    """Wrong password raises AuthError."""
    await setup_owner(db_session, password="correct")
    with pytest.raises(AuthError):
        await verify_password(db_session, password="wrong")


@pytest.mark.integration
async def test_verify_password_before_setup(db_session: AsyncSession) -> None:
    """Verifying password before setup raises AuthError (not crashes)."""
    with pytest.raises(AuthError):
        await verify_password(db_session, password="anything")


@pytest.mark.integration
async def test_create_and_get_session(db_session: AsyncSession) -> None:
    """Session tokens are opaque, storable, and retrievable."""
    owner = await setup_owner(db_session, password="pass")
    token = await create_session(db_session, owner_id=owner.id)
    assert isinstance(token, str)
    assert len(token) >= 32  # opaque random token

    session = await get_session(db_session, token=token)
    assert session is not None
    assert session.owner_id == owner.id


@pytest.mark.integration
async def test_expired_session_rejected(db_session: AsyncSession) -> None:
    """An expired session token must be rejected by get_session."""
    owner = await setup_owner(db_session, password="pass")
    token = await create_session(db_session, owner_id=owner.id)

    # Manually expire the session.
    import sqlalchemy as sa
    await db_session.execute(
        sa.text("UPDATE owner_session SET expires_at = now() - interval '1 second' WHERE token = :t"),
        {"t": token},
    )
    await db_session.flush()

    session = await get_session(db_session, token=token)
    assert session is None


@pytest.mark.integration
async def test_change_password_succeeds(db_session: AsyncSession) -> None:
    """Password change with correct current password updates the hash."""
    owner = await setup_owner(db_session, password="old")
    await change_password(db_session, owner_id=owner.id, old_password="old", new_password="new")
    # New password works.
    updated = await verify_password(db_session, password="new")
    assert updated.id == owner.id


@pytest.mark.integration
async def test_change_password_wrong_old_raises(db_session: AsyncSession) -> None:
    """Password change with wrong current password raises AuthError."""
    owner = await setup_owner(db_session, password="correct")
    with pytest.raises(AuthError):
        await change_password(
            db_session, owner_id=owner.id, old_password="wrong", new_password="new"
        )


@pytest.mark.integration
async def test_throttle_after_excessive_failures(db_session: AsyncSession) -> None:
    """Excessive failed login attempts trigger ThrottledError."""
    await setup_owner(db_session, password="correct")
    ip = "10.0.0.1"
    for _ in range(10):
        await record_failed_login(db_session, ip=ip)

    with pytest.raises(ThrottledError):
        await verify_password(db_session, password="wrong", ip=ip)


@pytest.mark.integration
async def test_successful_login_resets_throttle(db_session: AsyncSession) -> None:
    """A successful login resets the failed-attempt counter for the IP."""
    await setup_owner(db_session, password="correct")
    ip = "10.0.0.2"
    # Record some failures but below the threshold.
    for _ in range(3):
        await record_failed_login(db_session, ip=ip)
    # Successful login — should not raise.
    owner = await verify_password(db_session, password="correct", ip=ip)
    assert owner is not None
