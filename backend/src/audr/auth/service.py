"""Authentication service: setup, login, session, password-change, throttle."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audr.auth.models import LoginAttempt, Owner, Session

# Argon2id with OWASP-recommended parameters (RFC 9106 minimum config).
_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

SESSION_TTL = timedelta(hours=24)

# Throttle: max failed attempts allowed within the window.
THROTTLE_WINDOW = timedelta(minutes=15)
THROTTLE_MAX_FAILURES = 10


class AlreadySetupError(Exception):
    """setup_owner called when an owner already exists."""


class NotSetupError(Exception):
    """login called before any owner exists."""


class AuthenticationError(Exception):
    """Wrong password."""


class ThrottledError(Exception):
    """Too many failed login attempts; caller should return 429."""

    def __init__(self, retry_after: int = 900) -> None:
        super().__init__("Too many failed login attempts")
        self.retry_after = retry_after


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def is_setup(db: AsyncSession) -> bool:
    """Return True if the owner row exists."""
    result = await db.execute(select(Owner.id).limit(1))
    return result.scalar_one_or_none() is not None


async def _create_session(db: AsyncSession) -> Session:
    now = _utcnow()
    session = Session(
        id=uuid.uuid4(),
        csrf_token=secrets.token_hex(32),
        revoked=False,
        created_at=now,
        expires_at=now + SESSION_TTL,
        last_active_at=now,
    )
    db.add(session)
    await db.flush()
    return session


async def setup_owner(db: AsyncSession, password: str) -> tuple[UUID, str]:
    """Hash *password*, create the owner row, open the first session.

    Returns (session_id, csrf_token).
    Raises AlreadySetupError if an owner already exists.
    """
    hash_ = _hasher.hash(password)
    now = _utcnow()
    owner = Owner(
        id=uuid.uuid4(),
        singleton=True,
        argon2_hash=hash_,
        created_at=now,
        updated_at=now,
    )
    db.add(owner)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise AlreadySetupError from None
    session = await _create_session(db)
    await db.commit()
    return session.id, session.csrf_token


async def _is_throttled(db: AsyncSession) -> bool:
    window_start = _utcnow() - THROTTLE_WINDOW
    result = await db.execute(
        select(func.count())
        .select_from(LoginAttempt)
        .where(LoginAttempt.success.is_(False))
        .where(LoginAttempt.attempted_at >= window_start)
    )
    count: int = result.scalar_one()
    return count >= THROTTLE_MAX_FAILURES


async def _record_attempt(db: AsyncSession, *, success: bool) -> None:
    attempt = LoginAttempt(
        id=uuid.uuid4(),
        attempted_at=_utcnow(),
        success=success,
    )
    db.add(attempt)
    await db.flush()


async def login(db: AsyncSession, password: str) -> tuple[UUID, str]:
    """Verify *password*, create a session, return (session_id, csrf_token).

    Raises ThrottledError, NotSetupError, or AuthenticationError.
    """
    if await _is_throttled(db):
        raise ThrottledError()

    result = await db.execute(select(Owner).limit(1))
    owner = result.scalar_one_or_none()
    if owner is None:
        raise NotSetupError

    try:
        _hasher.verify(owner.argon2_hash, password)
    except VerifyMismatchError:
        await _record_attempt(db, success=False)
        await db.commit()
        raise AuthenticationError from None

    # Rehash if parameters changed.
    if _hasher.check_needs_rehash(owner.argon2_hash):
        owner.argon2_hash = _hasher.hash(password)
        owner.updated_at = _utcnow()

    await _record_attempt(db, success=True)
    session = await _create_session(db)
    await db.commit()
    return session.id, session.csrf_token


# Type alias used by audr.auth.dependencies — Session ORM row returned by get_session.
SessionRow = Session


async def get_session(db: AsyncSession, *, token: str) -> Session | None:
    """Return the session for *token* (the cookie value, interpreted as a UUID).

    Returns None if the token is not a valid UUID, or if no live session exists.
    Delegates to get_valid_session after parsing the UUID.
    """
    try:
        session_id = UUID(token)
    except ValueError:
        return None
    return await get_valid_session(db, session_id)


async def get_valid_session(db: AsyncSession, session_id: UUID) -> Session | None:
    """Return the session if it exists, is not expired, and is not revoked."""
    now = _utcnow()
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .where(Session.revoked.is_(False))
        .where(Session.expires_at > now)
    )
    session = result.scalar_one_or_none()
    if session is not None:
        session.last_active_at = now
        await db.commit()
    return session


async def revoke_session(db: AsyncSession, session_id: UUID) -> None:
    """Mark *session_id* as revoked (logout)."""
    await db.execute(
        update(Session).where(Session.id == session_id).values(revoked=True)
    )
    await db.commit()


async def change_password(
    db: AsyncSession, session_id: UUID, current_password: str, new_password: str
) -> None:
    """Change the owner password and revoke ALL sessions.

    Raises AuthenticationError if current_password is wrong.
    """
    result = await db.execute(select(Owner).limit(1))
    owner = result.scalar_one_or_none()
    if owner is None:
        raise AuthenticationError

    try:
        _hasher.verify(owner.argon2_hash, current_password)
    except VerifyMismatchError:
        raise AuthenticationError from None

    now = _utcnow()
    owner.argon2_hash = _hasher.hash(new_password)
    owner.updated_at = now
    await db.flush()

    # Revoke every session including the current one.
    await db.execute(update(Session).values(revoked=True))
    await db.commit()
