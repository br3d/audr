"""DB access for the news tracked-asset allow-list (AUD-515).

`news_tracked_asset` is the explicit opt-in list of symbols eligible for
portfolio-wide press matching and the GDELT breadth tier. Held-ness alone is
not enough: a held asset's symbol or name can collide with an English word or
an unrelated coin's name (a meme token ticked 'BITCOIN' absorbed every
Bitcoin headline on staging) — the same "mapping, not keyword guessing"
principle `snapshot_registry.py` already applies for Snapshot.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def list_tracked_asset_symbols(session: AsyncSession) -> set[str]:
    """Return the enabled allow-list, upper-cased for case-insensitive lookup."""
    result = await session.execute(
        sa.text("SELECT asset_symbol FROM news_tracked_asset WHERE enabled")
    )
    return {row[0].upper() for row in result}
