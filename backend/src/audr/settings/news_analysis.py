"""Global news-analysis gate setting: on/off + importance threshold (AUD-658).

Backed by `news_analysis_setting`, a one-row table (migration 0043) rather
than a `kind`-keyed one like `schedule`/`integration` — the founder's
decision (AUD-658 wake comment, card `bea3dbdc`) was one global flag, not a
per-asset or per-feed one, so there is nothing to key by. Revision-checked
the same way as those tables.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


class RevisionConflictError(Exception):
    """Raised when the supplied revision does not match the current revision."""


@dataclass(frozen=True)
class NewsAnalysisSetting:
    enabled: bool
    importance_threshold: int
    revision: int


async def get_news_analysis_setting(session: AsyncSession) -> NewsAnalysisSetting:
    """Return the current (always exactly one) setting row.

    Seeded by migration 0043 — this never returns None in a migrated
    database.
    """
    row = (
        await session.execute(
            sa.text("SELECT enabled, importance_threshold, revision FROM news_analysis_setting")
        )
    ).first()
    assert row is not None  # noqa: S101 -- seeded by migration 0043
    return NewsAnalysisSetting(enabled=row[0], importance_threshold=row[1], revision=row[2])


async def update_news_analysis_setting(
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    importance_threshold: int | None = None,
    expected_revision: int | None = None,
) -> NewsAnalysisSetting:
    """Update the gate flag and/or threshold.

    Raises:
        ValueError: importance_threshold is outside 0-100.
        RevisionConflictError: expected_revision is given and stale.
    """
    if importance_threshold is not None and not (0 <= importance_threshold <= 100):
        raise ValueError("importance_threshold must be between 0 and 100")

    current = await get_news_analysis_setting(session)
    if expected_revision is not None and current.revision != expected_revision:
        raise RevisionConflictError(
            f"revision conflict: expected {expected_revision}, found {current.revision}"
        )

    set_parts: list[str] = ["revision = revision + 1", "updated_at = now()"]
    params: dict = {}  # type: ignore[type-arg]
    if enabled is not None:
        set_parts.append("enabled = :enabled")
        params["enabled"] = enabled
    if importance_threshold is not None:
        set_parts.append("importance_threshold = :importance_threshold")
        params["importance_threshold"] = importance_threshold

    await session.execute(
        sa.text(f"UPDATE news_analysis_setting SET {', '.join(set_parts)}"),  # noqa: S608 -- set_parts is a fixed ":param" vocabulary; values are bound, never interpolated
        params,
    )
    await session.flush()
    return await get_news_analysis_setting(session)
