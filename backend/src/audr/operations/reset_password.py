"""Exceptional host password-reset command: revokes sessions, never replaces
encryption keys (T087 / US4).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import AsyncSession

# Same Argon2id parameters as auth.service (OWASP minimum config).
_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)


class ResetPasswordError(Exception):
    """Raised when the reset cannot proceed (no owner row found)."""


async def reset_password(session: AsyncSession, *, new_password: str) -> dict:  # type: ignore[type-arg]
    """Reset the owner password and revoke all sessions.

    Does NOT replace the encryption key (key_state is unchanged).
    Raises ResetPasswordError if no owner row exists.
    Raises ValueError if new_password is shorter than 12 characters.

    Returns: {"sessions_revoked": int, "password_changed": True}
    """
    # Step 1: Validate password length.
    if len(new_password) < 12:
        raise ValueError("password must be at least 12 characters")

    # Step 2: Fetch owner row.
    result = await session.execute(sa.text("SELECT id FROM owner LIMIT 1"))
    owner_row = result.first()
    if owner_row is None:
        raise ResetPasswordError("no owner found; call setup first")

    owner_id = owner_row[0]

    # Step 3: Hash the new password with Argon2id.
    new_hash = _hasher.hash(new_password)

    # Step 4: Update the owner row.
    now = datetime.now(UTC)
    await session.execute(
        sa.text("UPDATE owner SET argon2_hash = :hash, updated_at = :now WHERE id = :id"),
        {"hash": new_hash, "now": now, "id": owner_id},
    )

    # Step 5: Count non-revoked sessions before revoking them.
    result = await session.execute(sa.text("SELECT COUNT(*) FROM session WHERE revoked = false"))
    sessions_revoked: int = int(result.scalar() or 0)

    # Step 6: Mark all sessions as revoked.
    await session.execute(sa.text("UPDATE session SET revoked = true"))

    # key_state is intentionally NOT touched here — encryption keys must never
    # be replaced on password reset

    await session.commit()

    return {"sessions_revoked": sessions_revoked, "password_changed": True}
