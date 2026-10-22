"""Integration tests for the keyless news_refresh job (AUD-512).

No CoinGecko key is configured anywhere in this module — proving the
feed-registry + GDELT path produces news without one is the point.
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

from audr.jobs import news as news_module
from audr.jobs.news import handle_news_refresh
from tests.helpers import wallet_address_columns

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_gdelt_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real inter-query delay (AUD-515) — tests assert on call
    count and query shape, not on wall-clock spacing.
    """
    monkeypatch.setattr(news_module, "_GDELT_QUERY_DELAY_S", 0)


@pytest.fixture(autouse=True)
def _no_hackernews_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real inter-query delay (AUD-656) — same reason as GDELT's."""
    monkeypatch.setattr(news_module, "_HN_QUERY_DELAY_S", 0)


# A fixed recent-past date would silently fall outside the 90-day retention
# window as the test suite ages, and handle_news_refresh prunes on every
# call — a fixture dated "a day ago" would otherwise get inserted and
# immediately deleted again within the same run.
_RECENT = datetime.now(UTC) - timedelta(days=1)

_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>{entry_id}</id>
    <title>{title}</title>
    <link href="{url}" rel="alternate"/>
    <updated>{updated}</updated>
  </entry>
</feed>
"""


def _atom(entry_id: str, title: str, url: str) -> bytes:
    updated = _RECENT.strftime("%Y-%m-%dT%H:%M:%SZ")
    return _ATOM.format(entry_id=entry_id, title=title, url=url, updated=updated).encode()


# RSS 2.0 shape — what news.google.com/rss/search actually serves (AUD-654).
_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Google News</title>
    {items}
  </channel>
</rss>
"""

_RSS_ITEM = """
    <item>
      <title>{title}</title>
      <link>{url}</link>
      <guid>{guid}</guid>
      <pubDate>{pub_date}</pubDate>
    </item>
"""


def _rss(*entries: tuple[str, str, str]) -> bytes:
    """Build an RSS 2.0 body from (guid, title, url) tuples, all dated recent."""
    pub_date = _RECENT.strftime("%a, %d %b %Y %H:%M:%S GMT")
    items = "".join(
        _RSS_ITEM.format(title=title, url=url, guid=guid, pub_date=pub_date)
        for guid, title, url in entries
    )
    return _RSS.format(items=items).encode()


def _hn_hits(*entries: tuple[str, str, int, int]) -> dict:
    """Build an Algolia `search_by_date` body from (objectID, title, points,
    num_comments) tuples, all dated recent — what hn.algolia.com actually
    serves (AUD-656).
    """
    created_at_i = int(_RECENT.timestamp())
    return {
        "hits": [
            {
                "objectID": object_id,
                "title": title,
                "url": f"https://example.com/hn/{object_id}",
                "points": points,
                "num_comments": num_comments,
                "created_at_i": created_at_i,
            }
            for object_id, title, points, num_comments in entries
        ]
    }


# The allow-list seed from migration 0026 — held assets outside this set
# must never get press/GDELT coverage regardless of symbol collisions.
_SEED_TRACKED_SYMBOLS = ("WETH", "UNI", "AAVE", "LINK")


async def _purge_news_tables(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM news_feed_symbol"))
    await session.execute(text("DELETE FROM news_feed"))
    await session.execute(text("DELETE FROM snapshot_space"))
    await session.execute(text("DELETE FROM asset_news"))
    await session.execute(text("DELETE FROM balance_observation_invalidation"))
    await session.execute(text("DELETE FROM balance_observation"))
    # _SEED_TRACKED_SYMBOLS is a module-local constant, not user input.
    _seed_list = ", ".join(f"'{symbol}'" for symbol in _SEED_TRACKED_SYMBOLS)
    _delete_sql = f"DELETE FROM news_tracked_asset WHERE asset_symbol NOT IN ({_seed_list})"  # noqa: S608
    await session.execute(text(_delete_sql))


@pytest.fixture()
async def news_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """A session with real commit/rollback — not `db_session`'s rollback-only
    transaction.

    `handle_news_refresh` commits per tier (AUD-515 P2: a later tier's crash
    must not roll back an earlier tier's durable work). SQLAlchemy raises
    "Can't operate on closed transaction" if code under test calls
    `session.commit()` from inside the `session.begin()` block `db_session`
    wraps around every test — `test_provider_purge.py` hit the same
    constraint for `quotes.py`'s commit-on-provider-error path and worked
    around it the same way, by bypassing `db_session` and managing the
    session directly.

    Purging on both setup and teardown, with a real commit, matters here for
    a second reason: a committed tier is not undone by a plain rollback, so
    without this a test's held-asset fixtures (wallet + balance_observation)
    leak into every test that runs after it — including, if this file isn't
    the last to run, unrelated integration tests in other files.
    """
    async with db_session_factory() as session:
        await _purge_news_tables(session)
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await _purge_news_tables(session)
            await session.commit()


async def _insert_feed(
    session: AsyncSession,
    *,
    key: str,
    url: str,
    tier: str = "press",
    symbols: list[str] | None = None,
    keep_pattern: str | None = None,
    block_pattern: str | None = None,
) -> uuid.UUID:
    feed_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO news_feed (id, key, tier, url, keep_pattern, block_pattern)
            VALUES (:id, :key, :tier, :url, :keep_pattern, :block_pattern)
            """
        ),
        {
            "id": str(feed_id),
            "key": key,
            "tier": tier,
            "url": url,
            "keep_pattern": keep_pattern,
            "block_pattern": block_pattern,
        },
    )
    for symbol in symbols or []:
        await session.execute(
            text("INSERT INTO news_feed_symbol (feed_id, asset_symbol) VALUES (:feed_id, :symbol)"),
            {"feed_id": str(feed_id), "symbol": symbol},
        )
    await session.flush()
    return feed_id


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
    """Insert an asset with a non-zero balance, so _get_held_assets sees it."""
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


async def _track_asset(session: AsyncSession, *, symbol: str) -> None:
    """Opt *symbol* into the press/GDELT allow-list (AUD-515)."""
    await session.execute(
        text(
            "INSERT INTO news_tracked_asset (asset_symbol) VALUES (:symbol)"
            " ON CONFLICT (asset_symbol) DO NOTHING"
        ),
        {"symbol": symbol},
    )
    await session.flush()


async def _news_rows(session: AsyncSession, *, asset_id: uuid.UUID) -> list[dict]:
    rows = await session.execute(
        text(
            "SELECT source, external_id, title, url, news_site, published_at"
            " FROM asset_news WHERE asset_id = :id ORDER BY published_at"
        ),
        {"id": str(asset_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def test_inserts_news_with_no_api_key_configured(news_session: AsyncSession) -> None:
    """No `integration` row for coingecko exists anywhere in this test."""
    asset_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://blog.ethereum.org/en/feed.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://blog.ethereum.org/en/feed.xml").mock(
            return_value=Response(
                200,
                content=_atom("entry-1", "Upgrade ships", "https://blog.ethereum.org/1"),
                headers={"etag": '"v1"'},
            )
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "project:ethereum-blog"
    assert rows[0]["external_id"] == "entry-1"
    assert rows[0]["title"] == "Upgrade ships"


async def test_304_not_modified_recorded_as_success_no_duplicate(
    news_session: AsyncSession,
) -> None:
    asset_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    feed_id = await _insert_feed(
        news_session,
        key="k",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/feed.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(
            return_value=Response(200, content=_atom("e1", "T", "https://example.com/1"))
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=asset_id)) == 1

    # Force the feed due again (bypassing the per-feed throttle) and return 304.
    await news_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(return_value=Response(304))
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert len(rows) == 1

    health = (
        await news_session.execute(
            text("SELECT consecutive_failures, last_success_at FROM news_feed WHERE id = :id"),
            {"id": str(feed_id)},
        )
    ).one()
    assert health[0] == 0
    assert health[1] is not None


async def test_dedupe_on_repoll_of_unchanged_feed(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    feed_id = await _insert_feed(
        news_session,
        key="k",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/feed.xml",
    )

    body = _atom("same-entry", "Same title", "https://example.com/1")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    await news_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=asset_id)) == 1


async def test_feed_failure_increments_health_without_breaking_other_feeds(
    news_session: AsyncSession,
) -> None:
    asset_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    broken_id = await _insert_feed(
        news_session,
        key="broken",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/broken.xml",
    )
    await _insert_feed(
        news_session,
        key="working",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/working.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/broken.xml").mock(
            return_value=Response(200, content=b"<html><body>404 Not Found</body></html>")
        )
        mock.get("https://example.com/working.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "working"

    health = (
        await news_session.execute(
            text("SELECT consecutive_failures, last_error FROM news_feed WHERE id = :id"),
            {"id": str(broken_id)},
        )
    ).one()
    assert health[0] == 1
    assert health[1] is not None


async def test_feed_scoped_to_unheld_symbol_is_not_polled(news_session: AsyncSession) -> None:
    await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="weth-feed",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/weth.xml",
    )
    await _insert_feed(
        news_session,
        key="aave-feed",
        tier="project",
        symbols=["AAVE"],
        url="https://example.com/aave.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        weth_route = mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        aave_route = mock.get("https://example.com/aave.xml").mock(return_value=Response(200))
        await handle_news_refresh(news_session, uuid.uuid4())

    assert weth_route.called
    assert not aave_route.called


async def test_feed_with_two_symbols_inserts_one_row_per_asset(
    news_session: AsyncSession,
) -> None:
    """The AUD-571 case: a feed mapped to both ETH and WETH inserts the same
    item once per held asset, not once overall.
    """
    eth_id = await _hold_asset(news_session, symbol="ETH", name="Ether")
    weth_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH", "ETH"],
        url="https://example.com/eth.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/eth.xml").mock(
            return_value=Response(
                200, content=_atom("e1", "Upgrade ships", "https://example.com/1")
            )
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    eth_rows = await _news_rows(news_session, asset_id=eth_id)
    weth_rows = await _news_rows(news_session, asset_id=weth_id)
    assert len(eth_rows) == 1
    assert len(weth_rows) == 1
    assert eth_rows[0]["external_id"] == weth_rows[0]["external_id"] == "e1"


async def test_feed_with_two_symbols_dedupes_on_repoll(news_session: AsyncSession) -> None:
    eth_id = await _hold_asset(news_session, symbol="ETH", name="Ether")
    weth_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    feed_id = await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH", "ETH"],
        url="https://example.com/eth.xml",
    )

    body = _atom("same-entry", "Same title", "https://example.com/1")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/eth.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    await news_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/eth.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=eth_id)) == 1
    assert len(await _news_rows(news_session, asset_id=weth_id)) == 1


async def test_adding_symbol_to_feed_invalidates_conditional_get(
    news_session: AsyncSession,
) -> None:
    """AUD-573: a feed that already has an etag from polling WETH must not
    stay 304'd forever once ETH is mapped to it — the upstream body hasn't
    changed, but the new symbol needs the next poll to actually parse it.
    """
    weth_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    eth_id = await _hold_asset(news_session, symbol="ETH", name="Ether")
    feed_id = await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/eth.xml",
    )

    body = _atom("e1", "Upgrade ships", "https://example.com/1")

    def _conditional_response(request: object) -> Response:
        if request.headers.get("if-none-match") == '"v1"':
            return Response(304)
        return Response(200, content=body, headers={"etag": '"v1"'})

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/eth.xml").mock(side_effect=_conditional_response)
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=weth_id)) == 1
    assert await _news_rows(news_session, asset_id=eth_id) == []

    # The manual INSERT path AUD-568 part B drives — no helper call, no
    # explicit etag or last_polled_at reset by the caller. The feed was just
    # polled above, so last_polled_at is seconds old: if the trigger only
    # cleared etag/last_modified, list_due_feeds would still skip this feed
    # for the rest of its normal poll window.
    await news_session.execute(
        text("INSERT INTO news_feed_symbol (feed_id, asset_symbol) VALUES (:feed_id, 'ETH')"),
        {"feed_id": str(feed_id)},
    )
    await news_session.flush()

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get("https://example.com/eth.xml").mock(side_effect=_conditional_response)
        await handle_news_refresh(news_session, uuid.uuid4())

    assert "if-none-match" not in route.calls.last.request.headers
    assert len(await _news_rows(news_session, asset_id=eth_id)) == 1
    assert len(await _news_rows(news_session, asset_id=weth_id)) == 1


async def test_feed_where_no_mapped_symbol_is_held_is_not_polled(
    news_session: AsyncSession,
) -> None:
    await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="aave-feed",
        tier="project",
        symbols=["AAVE", "LINK"],
        url="https://example.com/aave.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        aave_route = mock.get("https://example.com/aave.xml").mock(return_value=Response(200))
        # WETH is seed-tracked (migration 0026) and has no native feed in
        # this test, so it falls back to the GDELT breadth tier — stub it.
        mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert not aave_route.called


async def test_press_tier_feed_matches_by_keyword(news_session: AsyncSession) -> None:
    weth_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _hold_asset(news_session, symbol="UNI", name="Uniswap")
    await _insert_feed(
        news_session, key="press:coindesk", tier="press", url="https://example.com/press.xml"
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/press.xml").mock(
            return_value=Response(
                200, content=_atom("p1", "WETH rallies today", "https://example.com/p1")
            )
        )
        # Neither WETH nor UNI has a project/release feed in this test, so
        # both fall back to the GDELT breadth tier — stub it out, empty.
        mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=weth_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "press:coindesk"


async def test_gdelt_breadth_tier_used_for_asset_without_native_feed(
    news_session: AsyncSession,
) -> None:
    asset_id = await _hold_asset(news_session, symbol="ZORB", name="Zorbcoin")
    await _track_asset(news_session, symbol="ZORB")

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(
                200,
                json={
                    "articles": [
                        {
                            "url": "https://news.example.com/zorb",
                            "title": "Zorbcoin breaks out",
                            "seendate": _RECENT.strftime("%Y%m%dT%H%M%SZ"),
                            "domain": "news.example.com",
                        }
                    ]
                },
            )
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "gdelt"
    assert rows[0]["url"] == "https://news.example.com/zorb"


async def test_gdelt_not_queried_for_asset_with_native_feed(news_session: AsyncSession) -> None:
    await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="weth-feed",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/weth.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert not gdelt_route.called


async def test_untracked_symbol_collision_gets_no_press_or_gdelt_news(
    news_session: AsyncSession,
) -> None:
    """Reproduces the staging bug (AUD-515 P1): a held airdrop token's
    on-chain ticker is literally 'BITCOIN'. It must not vacuum up every
    Bitcoin headline just because it is held and `match_assets` hits a
    word-boundary match on its symbol.
    """
    bitcoin_meme_id = await _hold_asset(
        news_session, symbol="BITCOIN", name="HarryPotterObamaSonic10Inu"
    )
    await _insert_feed(
        news_session, key="press:coindesk", tier="press", url="https://example.com/press.xml"
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/press.xml").mock(
            return_value=Response(
                200, content=_atom("p1", "Bitcoin hits new high", "https://example.com/p1")
            )
        )
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert await _news_rows(news_session, asset_id=bitcoin_meme_id) == []
    assert not gdelt_route.called


async def test_gdelt_tier_capped_at_max_queries_per_run(news_session: AsyncSession) -> None:
    """70 held assets must not translate into 70 sequential GDELT calls —
    the staging incident that burned ~13 minutes of a 900s cadence for
    zero rows (AUD-515 P1).
    """
    symbols = [f"ZZ{i}" for i in range(news_module._GDELT_MAX_QUERIES_PER_RUN + 5)]
    for symbol in symbols:
        await _hold_asset(news_session, symbol=symbol, name=symbol)
        await _track_asset(news_session, symbol=symbol)

    with respx.mock(assert_all_called=False) as mock:
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert gdelt_route.call_count == news_module._GDELT_MAX_QUERIES_PER_RUN


async def test_gdelt_tier_aborts_after_consecutive_429s(news_session: AsyncSession) -> None:
    """A run pinned against GDELT rate limits must back off rather than
    grind through every remaining asset one 429 at a time.
    """
    symbols = [f"ZZ{i}" for i in range(news_module._GDELT_MAX_CONSECUTIVE_429 + 3)]
    for symbol in symbols:
        await _hold_asset(news_session, symbol=symbol, name=symbol)
        await _track_asset(news_session, symbol=symbol)

    with respx.mock(assert_all_called=False) as mock:
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(429)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert gdelt_route.call_count == news_module._GDELT_MAX_CONSECUTIVE_429


async def test_gdelt_query_is_quoted_and_skips_short_symbols(news_session: AsyncSession) -> None:
    await _hold_asset(news_session, symbol="T", name="Threshold")
    await _track_asset(news_session, symbol="T")
    await _hold_asset(news_session, symbol="ZORB", name="Zorbcoin")
    await _track_asset(news_session, symbol="ZORB")

    with respx.mock(assert_all_called=False) as mock:
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert gdelt_route.call_count == 1
    sent = gdelt_route.calls.last.request
    assert sent.url.params["query"] == '"ZORB"'


async def test_feed_polls_durable_when_later_tier_raises(
    news_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash in the GDELT or Snapshot tier must not roll back feed polls
    and health counters that already committed (AUD-515 P2) — on staging,
    `last_polled_at` stayed NULL and `asset_news` stayed empty for the
    whole run despite feeds having been fetched successfully at the start.
    """
    asset_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    feed_id = await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/weth.xml",
    )
    # A due snapshot space is required so the (monkeypatched) Snapshot tier
    # actually runs this run — otherwise there is nothing for it to crash on.
    await news_session.execute(
        text("INSERT INTO snapshot_space (asset_symbol, space_id) VALUES ('WETH', 'ethereum.eth')")
    )

    async def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("simulated crash mid-run")

    monkeypatch.setattr(news_module, "_poll_snapshot_space", _boom)

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        with pytest.raises(RuntimeError, match="simulated crash mid-run"):
            await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert len(rows) == 1

    health = (
        await news_session.execute(
            text("SELECT last_polled_at, last_success_at FROM news_feed WHERE id = :id"),
            {"id": str(feed_id)},
        )
    ).one()
    assert health[0] is not None
    assert health[1] is not None


async def test_prune_removes_rows_older_than_90_days(news_session: AsyncSession) -> None:
    asset_id = await _insert_asset(news_session, symbol="OLD", name="Old Token")
    await news_session.execute(
        text(
            """
            INSERT INTO asset_news
              (asset_id, source, external_id, title, url, news_site, published_at)
            VALUES
              (:asset_id, 'gdelt', 'too-old', 'Too old', 'https://example.com/old', 'x',
               now() - interval '91 days'),
              (:asset_id, 'gdelt', 'boundary', 'Within window', 'https://example.com/new', 'x',
               now() - interval '89 days')
            """
        ),
        {"asset_id": str(asset_id)},
    )
    await news_session.flush()

    await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["boundary"]


async def test_feed_items_older_than_retention_are_not_inserted(
    news_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AUD-573 follow-up: a feed's full history must not round-trip through
    `_insert_news` only for `_prune_old_news` to delete it moments later in
    the same run — one live poll after the symbol-invalidation fix landed
    did exactly that: feeds_polled=2 inserted=1288 pruned=1272 for a 16-row
    effective gain.
    """
    asset_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/eth.xml",
    )

    calls = 0
    original_insert_news = news_module._insert_news

    async def _counting_insert_news(*args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        return await original_insert_news(*args, **kwargs)

    monkeypatch.setattr(news_module, "_insert_news", _counting_insert_news)

    old_updated = (datetime.now(UTC) - timedelta(days=91)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_updated = _RECENT.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>old-1</id>
    <title>Ancient upgrade</title>
    <link href="https://example.com/old" rel="alternate"/>
    <updated>{old_updated}</updated>
  </entry>
  <entry>
    <id>new-1</id>
    <title>Fresh upgrade</title>
    <link href="https://example.com/new" rel="alternate"/>
    <updated>{new_updated}</updated>
  </entry>
</feed>
""".encode()

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/eth.xml").mock(
            return_value=Response(200, content=body, headers={"etag": '"v1"'})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert calls == 1
    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["new-1"]


async def test_release_tier_noise_is_not_ingested(news_session: AsyncSession) -> None:
    """AUD-640: package tags and pre-release builds never reach the table.

    Filtered at ingestion, not in the API or the UI, so the row costs no
    retention, no alert pass and no card slot.
    """
    asset_id = await _hold_asset(news_session, symbol="ENS", name="Ethereum Name Service")
    await _insert_feed(
        news_session,
        key="release:ens-contracts",
        tier="release",
        symbols=["ENS"],
        url="https://example.com/ens.atom",
    )

    updated = _RECENT.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>pkg-1</id>
    <title>@ensdomains/ensjs@5.2.4</title>
    <link href="https://example.com/pkg" rel="alternate"/>
    <updated>{updated}</updated>
  </entry>
  <entry>
    <id>beta-1</id>
    <title>v2.10.2-beta5</title>
    <link href="https://example.com/beta" rel="alternate"/>
    <updated>{updated}</updated>
  </entry>
  <entry>
    <id>real-1</id>
    <title>ENS Name Wrapper v1.0</title>
    <link href="https://example.com/real" rel="alternate"/>
    <updated>{updated}</updated>
  </entry>
</feed>
""".encode()

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/ens.atom").mock(
            return_value=Response(200, content=body, headers={"etag": '"v1"'})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["real-1"]


async def test_non_release_tier_keeps_announcement_about_a_beta(
    news_session: AsyncSession,
) -> None:
    """The filter is release-tier only — a blog post about a beta is news."""
    asset_id = await _hold_asset(news_session, symbol="ENS", name="Ethereum Name Service")
    await _insert_feed(
        news_session,
        key="project:ens-blog",
        tier="project",
        symbols=["ENS"],
        url="https://example.com/ens-blog.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/ens-blog.xml").mock(
            return_value=Response(
                200,
                content=_atom("blog-1", "Our beta is live", "https://example.com/blog"),
                headers={"etag": '"v1"'},
            )
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["blog-1"]


# ---------------------------------------------------------------------------
# Feed-level keep/block patterns (AUD-655) — DB-sourced, any tier
# ---------------------------------------------------------------------------


async def test_feed_block_pattern_drops_matching_titles(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="ENS", name="Ethereum Name Service")
    await _insert_feed(
        news_session,
        key="project:ens-blog",
        tier="project",
        symbols=["ENS"],
        url="https://example.com/ens-blog.xml",
        block_pattern=r"sponsored",
    )

    body = _atom_many(
        ("real", "ENS Name Wrapper v1.0", "https://example.com/real"),
        ("ad", "Sponsored: win a free domain", "https://example.com/ad"),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/ens-blog.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["real"]


async def test_feed_keep_pattern_drops_titles_that_fail_it(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="ENS", name="Ethereum Name Service")
    await _insert_feed(
        news_session,
        key="project:ens-blog",
        tier="project",
        symbols=["ENS"],
        url="https://example.com/ens-blog.xml",
        keep_pattern=r"release|upgrade",
    )

    body = _atom_many(
        ("real", "ENS releases Name Wrapper v1.0", "https://example.com/real"),
        ("offtopic", "Join our community call", "https://example.com/offtopic"),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/ens-blog.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["real"]


async def test_feed_keep_and_block_patterns_combine(news_session: AsyncSession) -> None:
    """A title must pass keep_pattern, and then survive block_pattern —
    either one can drop it (AUD-655 req. 2)."""
    asset_id = await _hold_asset(news_session, symbol="ENS", name="Ethereum Name Service")
    await _insert_feed(
        news_session,
        key="project:ens-blog",
        tier="project",
        symbols=["ENS"],
        url="https://example.com/ens-blog.xml",
        keep_pattern=r"release|upgrade",
        block_pattern=r"beta|rc",
    )

    body = _atom_many(
        ("real", "ENS releases Name Wrapper v1.0", "https://example.com/real"),
        ("offtopic", "Join our community call", "https://example.com/offtopic"),
        ("prerelease", "ENS releases v1.1-beta upgrade", "https://example.com/prerelease"),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/ens-blog.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["real"]


async def test_feed_with_broken_block_pattern_does_not_break_the_run(
    news_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken regex stored on a feed (AUD-655 req. 4) must not crash
    `news_refresh` — the feed polls as if the rule were unset, logged so the
    bad pattern is visible, and does not stop the next feed from polling."""
    caplog.set_level("WARNING")
    asset_id = await _hold_asset(news_session, symbol="ENS", name="Ethereum Name Service")
    other_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        news_session,
        key="project:ens-blog",
        tier="project",
        symbols=["ENS"],
        url="https://example.com/ens-blog.xml",
        block_pattern=r"(unclosed",
    )
    await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/eth.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/ens-blog.xml").mock(
            return_value=Response(
                200,
                content=_atom("real", "ENS Name Wrapper v1.0", "https://example.com/real"),
            )
        )
        mock.get("https://example.com/eth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    # The broken pattern is treated as unset, not as "drop everything".
    assert [r["external_id"] for r in await _news_rows(news_session, asset_id=asset_id)] == ["real"]
    # The next feed in the same run is unaffected.
    assert [r["external_id"] for r in await _news_rows(news_session, asset_id=other_id)] == ["e1"]
    assert "project:ens-blog" in caplog.text
    assert "block" in caplog.text


def _atom_many(*entries: tuple[str, str, str]) -> bytes:
    updated = _RECENT.strftime("%Y-%m-%dT%H:%M:%SZ")
    entries_xml = "".join(
        f"<entry><id>{entry_id}</id><title>{title}</title>"
        f'<link href="{url}" rel="alternate"/><updated>{updated}</updated></entry>'
        for entry_id, title, url in entries
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">' + entries_xml + "</feed>"
    ).encode()


# ---------------------------------------------------------------------------
# Aggregator tier — keyless Google News breadth-tier (AUD-654)
# ---------------------------------------------------------------------------


async def test_aggregator_tier_inserts_matching_news(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="aggregator:sushi",
        tier="aggregator",
        symbols=["SUSHI"],
        url="https://news.google.com/rss/search?q=%22SushiSwap%22+crypto",
    )

    body = _rss(("g1", "SushiSwap exploit drains $3M from liquidity pool", "https://example.com/1"))
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://news.google.com/rss/search?q=%22SushiSwap%22+crypto").mock(
            return_value=Response(200, content=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["g1"]
    assert rows[0]["source"] == "aggregator:sushi"


async def test_aggregator_tier_drops_titles_that_do_not_mention_the_asset(
    news_session: AsyncSession,
) -> None:
    """Relevance gate (AUD-654 req. 4): a quoted search is not attribution-exact
    the way a project blog is, so a result that never says the symbol or the
    project name must not land on the card.
    """
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="aggregator:sushi",
        tier="aggregator",
        symbols=["SUSHI"],
        url="https://example.com/aggregator-sushi.xml",
    )

    body = _rss(
        ("on-topic", "SushiSwap ships v3 router upgrade", "https://example.com/1"),
        ("off-topic", "Global markets rally on rate-cut hopes", "https://example.com/2"),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/aggregator-sushi.xml").mock(
            return_value=Response(200, content=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["on-topic"]


async def test_aggregator_tier_filters_evergreen_explainer_noise(
    news_session: AsyncSession,
) -> None:
    """The live SUSHI measurement from the issue: the newest Google News row
    was an evergreen explainer, not news, and must never be ingested.

    The rule itself is data now (AUD-655) — this feed's `block_pattern` is
    the mechanism under test, not a code constant.
    """
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="aggregator:sushi",
        tier="aggregator",
        symbols=["SUSHI"],
        url="https://example.com/aggregator-sushi.xml",
        block_pattern=r"^(?:what\s+is|how\s+does)\b",
    )

    body = _rss(
        ("explainer", "What Is SushiSwap and How Does It Work? — Binance", "https://example.com/1"),
        ("real", "SushiSwap exploit drains $3M from liquidity pool", "https://example.com/2"),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/aggregator-sushi.xml").mock(
            return_value=Response(200, content=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["real"]


async def test_aggregator_tier_dedupes_on_repoll(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    feed_id = await _insert_feed(
        news_session,
        key="aggregator:sushi",
        tier="aggregator",
        symbols=["SUSHI"],
        url="https://example.com/aggregator-sushi.xml",
    )

    body = _rss(("g1", "SushiSwap exploit drains $3M from liquidity pool", "https://example.com/1"))
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/aggregator-sushi.xml").mock(
            return_value=Response(200, content=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    await news_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/aggregator-sushi.xml").mock(
            return_value=Response(200, content=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=asset_id)) == 1


async def test_aggregator_tier_failure_does_not_break_other_feeds(
    news_session: AsyncSession,
) -> None:
    """A 429 or an HTML error page from Google News must degrade to skip-and-log,
    same as any other feed (AUD-654 req. 7) — it must not break the run.
    """
    sushi_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    weth_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    broken_id = await _insert_feed(
        news_session,
        key="aggregator:sushi",
        tier="aggregator",
        symbols=["SUSHI"],
        url="https://example.com/aggregator-sushi.xml",
    )
    await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/weth.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/aggregator-sushi.xml").mock(return_value=Response(429))
        mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert await _news_rows(news_session, asset_id=sushi_id) == []
    assert len(await _news_rows(news_session, asset_id=weth_id)) == 1
    health = (
        await news_session.execute(
            text("SELECT consecutive_failures FROM news_feed WHERE id = :id"),
            {"id": str(broken_id)},
        )
    ).one()
    assert health[0] == 1


async def test_gdelt_not_queried_for_asset_with_aggregator_feed(
    news_session: AsyncSession,
) -> None:
    """AUD-654 req. 6: an aggregator feed is coverage too, so it must not also
    burn one of GDELT's scarce per-run query slots for the same symbol.
    """
    await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _track_asset(news_session, symbol="SUSHI")
    await _insert_feed(
        news_session,
        key="aggregator:sushi",
        tier="aggregator",
        symbols=["SUSHI"],
        url="https://example.com/aggregator-sushi.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/aggregator-sushi.xml").mock(
            return_value=Response(200, content=_rss())
        )
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert not gdelt_route.called


async def test_hackernews_tier_inserts_matching_engaged_story(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )

    body = _hn_hits(("1", "SushiSwap exploit drains $3M from liquidity pool", 10, 4))
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["1"]
    assert rows[0]["source"] == "hackernews"
    assert rows[0]["news_site"] == "hackernews"


async def test_hackernews_tier_drops_story_with_no_engagement(news_session: AsyncSession) -> None:
    """The engagement gate (AUD-656 req. 3): a story sitting at its
    submitter's own vote with zero comments is noise, not news.
    """
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )

    body = _hn_hits(
        ("flat", "SushiSwap integration announced", 1, 0),
        ("engaged", "SushiSwap exploit drains $3M from liquidity pool", 1, 1),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["engaged"]


async def test_hackernews_tier_drops_titles_that_do_not_mention_the_asset(
    news_session: AsyncSession,
) -> None:
    """Relevance gate: Algolia's search is typo-tolerant, not attribution-exact
    — a result that never says the symbol or the project name must not land
    on the card.
    """
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )

    body = _hn_hits(
        ("on-topic", "SushiSwap ships v3 router upgrade", 20, 5),
        ("off-topic", "Global markets rally on rate-cut hopes", 50, 10),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["on-topic"]


async def test_hackernews_tier_block_pattern_drops_matching_titles(
    news_session: AsyncSession,
) -> None:
    """Noise is data here, not code (AUD-656 req. — same mechanism as
    AUD-655): a project-name collision an operator finds live is silenced
    with `block_pattern`, no deploy required.
    """
    asset_id = await _hold_asset(news_session, symbol="MATIC", name="Polygon")
    await _insert_feed(
        news_session,
        key="hackernews:matic",
        tier="hackernews",
        symbols=["MATIC"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22Polygon%22",
        block_pattern=r"polygon\.com|polygon\s+(?:clipping|region|intersection)",
    )

    body = _hn_hits(
        ("gaming-site", "Polygon.com launches new review format", 50, 20),
        ("real", "Polygon network outage traced to validator bug", 15, 8),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    rows = await _news_rows(news_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["real"]


async def test_hackernews_tier_dedupes_on_repoll(news_session: AsyncSession) -> None:
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    feed_id = await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )

    body = _hn_hits(("1", "SushiSwap exploit drains $3M from liquidity pool", 10, 4))
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    await news_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert len(await _news_rows(news_session, asset_id=asset_id)) == 1


async def test_hackernews_tier_failure_does_not_break_other_feeds(
    news_session: AsyncSession,
) -> None:
    sushi_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    weth_id = await _hold_asset(news_session, symbol="WETH", name="Wrapped Ether")
    broken_id = await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )
    await _insert_feed(
        news_session,
        key="project:ethereum-blog",
        tier="project",
        symbols=["WETH"],
        url="https://example.com/weth.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=Response(503))
        mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert await _news_rows(news_session, asset_id=sushi_id) == []
    assert len(await _news_rows(news_session, asset_id=weth_id)) == 1
    health = (
        await news_session.execute(
            text("SELECT consecutive_failures, last_error FROM news_feed WHERE id = :id"),
            {"id": str(broken_id)},
        )
    ).one()
    assert health[0] == 1
    assert health[1] is not None


async def test_hackernews_tier_capped_at_max_queries_per_run(news_session: AsyncSession) -> None:
    """A fresh install (or a migration run) must not translate N held assets
    into N sequential HN queries in one `news_refresh` call — same reasoning
    as GDELT's cap (AUD-515 P1), applied to this tier's own budget constant.
    """
    symbols = [f"ZZ{i}" for i in range(news_module._HN_MAX_QUERIES_PER_RUN + 5)]
    called_urls = []
    with respx.mock(assert_all_called=False) as mock:
        for i, symbol in enumerate(symbols):
            await _hold_asset(news_session, symbol=symbol, name=f"Zorbcoin{i}")
            url = f"https://hn.algolia.com/api/v1/search_by_date?query={symbol}"
            await _insert_feed(
                news_session,
                key=f"hackernews:{symbol.lower()}",
                tier="hackernews",
                symbols=[symbol],
                url=url,
            )
            route = mock.get(url).mock(return_value=Response(200, json=_hn_hits()))
            called_urls.append((url, route))
        await handle_news_refresh(news_session, uuid.uuid4())

    call_count = sum(1 for _url, route in called_urls if route.called)
    assert call_count == news_module._HN_MAX_QUERIES_PER_RUN


async def test_hackernews_tier_aborts_after_consecutive_429s(news_session: AsyncSession) -> None:
    symbols = [f"ZZ{i}" for i in range(news_module._HN_MAX_CONSECUTIVE_429 + 3)]
    routes = []
    with respx.mock(assert_all_called=False) as mock:
        for i, symbol in enumerate(symbols):
            await _hold_asset(news_session, symbol=symbol, name=f"Zorbcoin{i}")
            url = f"https://hn.algolia.com/api/v1/search_by_date?query={symbol}"
            await _insert_feed(
                news_session,
                key=f"hackernews:{symbol.lower()}",
                tier="hackernews",
                symbols=[symbol],
                url=url,
            )
            routes.append(mock.get(url).mock(return_value=Response(429)))
        await handle_news_refresh(news_session, uuid.uuid4())

    call_count = sum(1 for route in routes if route.called)
    assert call_count == news_module._HN_MAX_CONSECUTIVE_429


async def test_hackernews_items_older_than_retention_are_not_inserted(
    news_session: AsyncSession,
) -> None:
    asset_id = await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )

    too_old = int((_RECENT - timedelta(days=200)).timestamp())
    body = {
        "hits": [
            {
                "objectID": "old",
                "title": "SushiSwap launch announcement",
                "url": "https://example.com/old",
                "points": 50,
                "num_comments": 20,
                "created_at_i": too_old,
            }
        ]
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=body)
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert await _news_rows(news_session, asset_id=asset_id) == []


async def test_gdelt_not_queried_for_asset_with_hackernews_feed(
    news_session: AsyncSession,
) -> None:
    """A hackernews feed is coverage too (same as the aggregator tier,
    AUD-654 req. 6) — it must not also burn one of GDELT's scarce per-run
    query slots for the same symbol.
    """
    await _hold_asset(news_session, symbol="SUSHI", name="SushiSwap")
    await _track_asset(news_session, symbol="SUSHI")
    await _insert_feed(
        news_session,
        key="hackernews:sushi",
        tier="hackernews",
        symbols=["SUSHI"],
        url="https://hn.algolia.com/api/v1/search_by_date?query=%22SushiSwap%22",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://hn.algolia.com/api/v1/search_by_date").mock(
            return_value=Response(200, json=_hn_hits())
        )
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(news_session, uuid.uuid4())

    assert not gdelt_route.called
