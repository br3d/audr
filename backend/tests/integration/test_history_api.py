"""HTTP-level tests for GET /api/v1/history period validation (AUD-376).

Covers the API contract only (status codes, echoed period); point-selection
and thinning behaviour is covered at the query_history level in
test_history.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM owner"))


@pytest.mark.parametrize("period", ["24h", "7d", "30d", "90d", "1y", "all"])
async def test_history_accepts_all_known_periods(seeded_client, period: str) -> None:
    client, _csrf = seeded_client
    r = await client.get("/api/v1/history", params={"period": period})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period"] == period
    assert isinstance(body["items"], list)


async def test_history_rejects_unknown_period(seeded_client) -> None:
    client, _csrf = seeded_client
    r = await client.get("/api/v1/history", params={"period": "3y"})
    assert r.status_code == 422
