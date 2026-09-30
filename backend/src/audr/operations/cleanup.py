"""Periodic cleanup of unbounded auth audit tables (AUD-322).

login_attempt rows are written on every login attempt (success or failure)
and session rows on every login, but nothing ever deletes them — both tables
grow without bound. Retention here is generous relative to the
security-relevant windows (a 15-minute throttle window, a 24-hour session
TTL) so recent history stays available for troubleshooting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

LOGIN_ATTEMPT_RETENTION = timedelta(days=7)
SESSION_RETENTION = timedelta(days=7)


async def cleanup_expired_auth_rows(session: AsyncSession) -> dict[str, int]:
    """Delete login_attempt rows and expired session rows past their
    retention window. Returns the number of rows deleted per table."""
    now = datetime.now(UTC)

    login_result = await session.execute(
        sa.text("DELETE FROM login_attempt WHERE attempted_at < :cutoff"),
        {"cutoff": now - LOGIN_ATTEMPT_RETENTION},
    )
    session_result = await session.execute(
        sa.text("DELETE FROM session WHERE expires_at < :cutoff"),
        {"cutoff": now - SESSION_RETENTION},
    )
    return {
        "login_attempt": login_result.rowcount or 0,
        "session": session_result.rowcount or 0,
    }
