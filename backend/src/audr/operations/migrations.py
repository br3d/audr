"""Migration readiness check against the alembic head revision (T082 / US4)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


def _get_head_revision() -> str | None:
    """Return the current alembic head revision by reading the script directory.

    Walks up from this file's location looking for ``alembic.ini``.  Returns
    None if alembic is unavailable or no migrations exist.
    """
    try:
        from alembic.config import Config  # type: ignore[import-untyped]
        from alembic.script import ScriptDirectory  # type: ignore[import-untyped]

        here = Path(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "alembic.ini"
            if candidate.exists():
                cfg = Config(str(candidate))
                script = ScriptDirectory.from_config(cfg)
                heads = script.get_heads()
                return heads[0] if heads else None
    except Exception:  # pragma: no cover
        pass
    return None  # pragma: no cover


async def check_migration_readiness(session: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Check whether the DB schema is at the alembic head revision.

    Returns::

        {
            "up_to_date": bool,
            "current": str | None,   # version applied to the DB
            "head": str | None,      # latest revision known to alembic
        }
    """
    result = await session.execute(
        sa.text("SELECT version_num FROM alembic_version LIMIT 1")
    )
    row = result.first()
    current: str | None = row[0] if row is not None else None
    head: str | None = _get_head_revision()

    up_to_date: bool = (
        current is not None
        and head is not None
        and current == head
    )

    return {
        "up_to_date": up_to_date,
        "current": current,
        "head": head,
    }
