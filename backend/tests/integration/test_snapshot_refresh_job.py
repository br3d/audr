"""Integration tests for the Snapshot governance tier of news_refresh (AUD-513).

Proves the Snapshot source matches held assets by explicit `snapshot_space`
mapping rather than by symbol/keyword — the failure mode docs/news-sources.md
§3 calls out (an unrelated headline containing the word "zoo" must never be
treated as news about a ZOO holding).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs.news import handle_news_refresh
from tests.helpers import wallet_address_columns

pytestmark = pytest.mark.integration

_SNAPSHOT_URL = "https://hub.snapshot.org/graphql"
_GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

_RECENT = datetime.now(UTC) - timedelta(days=1)


def _proposal(
    *, proposal_id: str = "0xabc", title: str = "Raise the fee", state: str = "active"
) -> dict:
    return {
        "id": proposal_id,
        "title": title,
        "body": "Summary.",
        "state": state,
        "start": int(_RECENT.timestamp()),
        "end": int((_RECENT + timedelta(days=7)).timestamp()),
    }


def _graphql_response(proposals: list[dict]) -> dict:
    return {"data": {"proposals": proposals}}


async def _purge_tables(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM news_feed"))
    await session.execute(text("DELETE FROM snapshot_space"))
    await session.execute(text("DELETE FROM asset_news"))
    await session.execute(text("DELETE FROM balance_observation_invalidation"))
    await session.execute(text("DELETE FROM balance_observation"))


@pytest.fixture()
async def news_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """A session with real commit/rollback — not `db_session`'s rollback-only
    transaction.

    `handle_news_refresh` commits per tier (AUD-515 P2), and SQLAlchemy
    raises "Can't operate on closed transaction" if code under test calls
    `session.commit()` from inside the `session.begin()` block `db_session`
    wraps around every test (same constraint `test_news_refresh_job.py`'s
    `news_session` fixture works around, and `test_provider_purge.py` hit
    first for `quotes.py`'s commit-on-provider-error path). Purge on both
    setup and teardown, with a real commit, so a committed tier does not
    leak into the next test or into unrelated files that run later in the
    suite — a plain rollback cannot undo a commit.
    """
    async with db_session_factory() as session:
        await _purge_tables(session)
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await _purge_tables(session)
            await session.commit()


async def _insert_space(
    session: AsyncSession, *, asset_symbol: str, space_id: str = "uniswap"
) -> uuid.UUID:
    row_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO snapshot_space (id, asset_symbol, space_id)
            VALUES (:id, :asset_symbol, :space_id)
            """
        ),
        {"id": str(row_id), "asset_symbol": asset_symbol, "space_id": space_id},
    )
    await session.flush()
    return row_id


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    cols = await wallet_address_columns(session, wallet_id, address)
    await session.execute(
        text(
            "INSERT INTO wallet (id, address_ciphertext, address_bidx, label_ciphertext, status)"
            " VALUES (:id, :addr_ct, :addr_bidx, '', 'active')"
        ),
        {
            "id": str(wallet_id),
            "addr_ct": cols["address_ciphertext"],
            "addr_bidx": cols["address_bidx"],
        },
    )
    return wallet_id


async def _insert_asset(
    session: AsyncSession, *, symbol: str, name: str | None = None
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source)
            VALUES (:id, :addr, :sym, :name, 18, 'manual')
            """
        ),
        {
            "id": str(asset_id),
            "addr": "0x" + uuid.uuid4().hex[:40],
            "sym": symbol,
            "name": name or symbol,
        },
    )
    return asset_id


async def _hold_asset(session: AsyncSession, *, symbol: str, name: str | None = None) -> uuid.UUID:
    wallet_id = await _insert_wallet(session, "0x" + uuid.uuid4().hex[:40])
    asset_id = await _insert_asset(session, symbol=symbol, name=name)
    await session.execute(
        text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, 1000000000000000000, 100, now())"
        ),
        {"id": str(uuid.uuid4()), "wallet": str(wallet_id), "asset": str(asset_id)},
    )
    await session.flush()
    return asset_id


async def _force_space_due(session: AsyncSession, space_id: uuid.UUID) -> None:
    await session.execute(
        text("UPDATE snapshot_space SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": space_id},
    )


async def _news_rows(session: AsyncSession, *, asset_id: uuid.UUID) -> list[dict]:
    rows = await session.execute(
        text(
            "SELECT source, external_id, title, url FROM asset_news"
            " WHERE asset_id = :id ORDER BY published_at"
        ),
        {"id": str(asset_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def test_snapshot_proposal_inserted_for_mapped_held_asset(news_session: AsyncSession) -> None:
    uni_id = await _hold_asset(news_session, symbol="UNI", name="Uniswap")
    await _insert_space(news_session, asset_symbol="UNI", space_id="uniswap")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(_SNAPSHOT_URL).mock(
            return_value=Response(200, json=_graphql_response([_proposal()]))
        )
        mock.get(_GDELT_URL).mock(return_value=Response(200, json={"articles": []}))
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=uni_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "snapshot"
    assert rows[0]["external_id"] == "0xabc"
    assert rows[0]["title"] == "[active] Raise the fee"


async def test_snapshot_state_transition_updates_in_place_no_duplicate(
    news_session: AsyncSession,
) -> None:
    uni_id = await _hold_asset(news_session, symbol="UNI", name="Uniswap")
    space_id = await _insert_space(news_session, asset_symbol="UNI", space_id="uniswap")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(_SNAPSHOT_URL).mock(
            return_value=Response(200, json=_graphql_response([_proposal(state="active")]))
        )
        mock.get(_GDELT_URL).mock(return_value=Response(200, json={"articles": []}))
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=uni_id)) == 1

    await _force_space_due(news_session, space_id)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_SNAPSHOT_URL).mock(
            return_value=Response(200, json=_graphql_response([_proposal(state="closed")]))
        )
        mock.get(_GDELT_URL).mock(return_value=Response(200, json={"articles": []}))
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=uni_id)
    assert len(rows) == 1
    assert rows[0]["title"] == "[closed] Raise the fee"


async def test_snapshot_unmapped_asset_yields_nothing(news_session: AsyncSession) -> None:
    """A space mapped to a symbol we don't currently hold is not even polled."""
    zoo_id = await _hold_asset(news_session, symbol="ZOO", name="Zoocoin")
    await _insert_space(news_session, asset_symbol="UNI", space_id="uniswap")

    with respx.mock(assert_all_called=False) as mock:
        snapshot_route = mock.post(_SNAPSHOT_URL).mock(
            return_value=Response(200, json=_graphql_response([_proposal()]))
        )
        mock.get(_GDELT_URL).mock(return_value=Response(200, json={"articles": []}))
        await handle_news_refresh(news_session, uuid.uuid4())

    assert not snapshot_route.called
    assert await _news_rows(news_session, asset_id=zoo_id) == []


async def test_snapshot_adapter_error_swallowed_and_counted(news_session: AsyncSession) -> None:
    uni_id = await _hold_asset(news_session, symbol="UNI", name="Uniswap")
    space_id = await _insert_space(news_session, asset_symbol="UNI", space_id="uniswap")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(_SNAPSHOT_URL).mock(return_value=Response(500))
        mock.get(_GDELT_URL).mock(return_value=Response(200, json={"articles": []}))
        await handle_news_refresh(news_session, uuid.uuid4())

    assert await _news_rows(news_session, asset_id=uni_id) == []
    health = (
        await news_session.execute(
            text("SELECT consecutive_failures, last_error FROM snapshot_space WHERE id = :id"),
            {"id": str(space_id)},
        )
    ).one()
    assert health[0] == 1
    assert health[1] is not None


async def test_snapshot_does_not_use_symbol_keyword_matching(news_session: AsyncSession) -> None:
    """A proposal title containing the word "zoo" must never attach to the
    ZOO holding — Snapshot matching is by explicit space mapping only.
    """
    uni_id = await _hold_asset(news_session, symbol="UNI", name="Uniswap")
    zoo_id = await _hold_asset(news_session, symbol="ZOO", name="Zoocoin")
    await _insert_space(news_session, asset_symbol="UNI", space_id="uniswap")

    with respx.mock(assert_all_called=False) as mock:
        mock.post(_SNAPSHOT_URL).mock(
            return_value=Response(
                200,
                json=_graphql_response([_proposal(title="Fund the ZOO integration grant")]),
            )
        )
        mock.get(_GDELT_URL).mock(return_value=Response(200, json={"articles": []}))
        await handle_news_refresh(news_session, uuid.uuid4())

    uni_rows = await _news_rows(news_session, asset_id=uni_id)
    zoo_rows = await _news_rows(news_session, asset_id=zoo_id)
    assert len(uni_rows) == 1
    assert uni_rows[0]["source"] == "snapshot"
    assert zoo_rows == []
