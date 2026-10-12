"""Integration tests for the keyless news_refresh job (AUD-512).

No CoinGecko key is configured anywhere in this module — proving the
feed-registry + GDELT path produces news without one is the point.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.news import handle_news_refresh
from tests.helpers import wallet_address_columns

pytestmark = pytest.mark.integration

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


@pytest.fixture(autouse=True)
async def _clean_tables(db_session: AsyncSession) -> None:
    """Start from a clean slate within this test's own (rolled-back)
    transaction. Other test modules commit held-asset fixtures (e.g. UNI/WETH
    via `seeded_assets` + a balance_observation) that would otherwise leak
    into `_get_held_assets` here and make GDELT/feed-matching assertions
    order-dependent on the rest of the suite.
    """
    await db_session.execute(text("DELETE FROM news_feed"))
    await db_session.execute(text("DELETE FROM snapshot_space"))
    await db_session.execute(text("DELETE FROM asset_news"))
    await db_session.execute(text("DELETE FROM balance_observation_invalidation"))
    await db_session.execute(text("DELETE FROM balance_observation"))


async def _insert_feed(
    db_session: AsyncSession,
    *,
    key: str,
    url: str,
    tier: str = "press",
    asset_symbol: str | None = None,
) -> uuid.UUID:
    feed_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO news_feed (id, key, tier, asset_symbol, url)
            VALUES (:id, :key, :tier, :asset_symbol, :url)
            """
        ),
        {
            "id": str(feed_id),
            "key": key,
            "tier": tier,
            "asset_symbol": asset_symbol,
            "url": url,
        },
    )
    await db_session.flush()
    return feed_id


async def _insert_wallet(db_session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    cols = await wallet_address_columns(db_session, wallet_id, address)
    await db_session.execute(
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
    db_session: AsyncSession, *, symbol: str, name: str | None = None
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await db_session.execute(
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


async def _hold_asset(
    db_session: AsyncSession, *, symbol: str, name: str | None = None
) -> uuid.UUID:
    """Insert an asset with a non-zero balance, so _get_held_assets sees it."""
    wallet_id = await _insert_wallet(db_session, "0x" + uuid.uuid4().hex[:40])
    asset_id = await _insert_asset(db_session, symbol=symbol, name=name)
    await db_session.execute(
        text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, 1000000000000000000, 100, now())"
        ),
        {"id": str(uuid.uuid4()), "wallet": str(wallet_id), "asset": str(asset_id)},
    )
    await db_session.flush()
    return asset_id


async def _news_rows(db_session: AsyncSession, *, asset_id: uuid.UUID) -> list[dict]:
    rows = await db_session.execute(
        text(
            "SELECT source, external_id, title, url, news_site, published_at"
            " FROM asset_news WHERE asset_id = :id ORDER BY published_at"
        ),
        {"id": str(asset_id)},
    )
    return [dict(r) for r in rows.mappings()]


async def test_inserts_news_with_no_api_key_configured(db_session: AsyncSession) -> None:
    """No `integration` row for coingecko exists anywhere in this test."""
    asset_id = await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        db_session,
        key="project:ethereum-blog",
        tier="project",
        asset_symbol="WETH",
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
        await handle_news_refresh(db_session, uuid.uuid4())

    rows = await _news_rows(db_session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "project:ethereum-blog"
    assert rows[0]["external_id"] == "entry-1"
    assert rows[0]["title"] == "Upgrade ships"


async def test_304_not_modified_recorded_as_success_no_duplicate(db_session: AsyncSession) -> None:
    asset_id = await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    feed_id = await _insert_feed(
        db_session,
        key="k",
        tier="project",
        asset_symbol="WETH",
        url="https://example.com/feed.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(
            return_value=Response(200, content=_atom("e1", "T", "https://example.com/1"))
        )
        await handle_news_refresh(db_session, uuid.uuid4())

    assert len(await _news_rows(db_session, asset_id=asset_id)) == 1

    # Force the feed due again (bypassing the per-feed throttle) and return 304.
    await db_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(return_value=Response(304))
        await handle_news_refresh(db_session, uuid.uuid4())

    rows = await _news_rows(db_session, asset_id=asset_id)
    assert len(rows) == 1

    health = (
        await db_session.execute(
            text("SELECT consecutive_failures, last_success_at FROM news_feed WHERE id = :id"),
            {"id": str(feed_id)},
        )
    ).one()
    assert health[0] == 0
    assert health[1] is not None


async def test_dedupe_on_repoll_of_unchanged_feed(db_session: AsyncSession) -> None:
    asset_id = await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    feed_id = await _insert_feed(
        db_session,
        key="k",
        tier="project",
        asset_symbol="WETH",
        url="https://example.com/feed.xml",
    )

    body = _atom("same-entry", "Same title", "https://example.com/1")
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(db_session, uuid.uuid4())

    await db_session.execute(
        text("UPDATE news_feed SET last_polled_at = now() - interval '1 day' WHERE id = :id"),
        {"id": str(feed_id)},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/feed.xml").mock(return_value=Response(200, content=body))
        await handle_news_refresh(db_session, uuid.uuid4())

    assert len(await _news_rows(db_session, asset_id=asset_id)) == 1


async def test_feed_failure_increments_health_without_breaking_other_feeds(
    db_session: AsyncSession,
) -> None:
    asset_id = await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    broken_id = await _insert_feed(
        db_session,
        key="broken",
        tier="project",
        asset_symbol="WETH",
        url="https://example.com/broken.xml",
    )
    await _insert_feed(
        db_session,
        key="working",
        tier="project",
        asset_symbol="WETH",
        url="https://example.com/working.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/broken.xml").mock(
            return_value=Response(200, content=b"<html><body>404 Not Found</body></html>")
        )
        mock.get("https://example.com/working.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        await handle_news_refresh(db_session, uuid.uuid4())

    rows = await _news_rows(db_session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "working"

    health = (
        await db_session.execute(
            text("SELECT consecutive_failures, last_error FROM news_feed WHERE id = :id"),
            {"id": str(broken_id)},
        )
    ).one()
    assert health[0] == 1
    assert health[1] is not None


async def test_feed_scoped_to_unheld_symbol_is_not_polled(db_session: AsyncSession) -> None:
    await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        db_session,
        key="weth-feed",
        tier="project",
        asset_symbol="WETH",
        url="https://example.com/weth.xml",
    )
    await _insert_feed(
        db_session,
        key="aave-feed",
        tier="project",
        asset_symbol="AAVE",
        url="https://example.com/aave.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        weth_route = mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        aave_route = mock.get("https://example.com/aave.xml").mock(return_value=Response(200))
        await handle_news_refresh(db_session, uuid.uuid4())

    assert weth_route.called
    assert not aave_route.called


async def test_press_tier_feed_matches_by_keyword(db_session: AsyncSession) -> None:
    weth_id = await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    await _hold_asset(db_session, symbol="UNI", name="Uniswap")
    await _insert_feed(
        db_session, key="press:coindesk", tier="press", url="https://example.com/press.xml"
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
        await handle_news_refresh(db_session, uuid.uuid4())

    rows = await _news_rows(db_session, asset_id=weth_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "press:coindesk"


async def test_gdelt_breadth_tier_used_for_asset_without_native_feed(
    db_session: AsyncSession,
) -> None:
    asset_id = await _hold_asset(db_session, symbol="ZORB", name="Zorbcoin")

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
        await handle_news_refresh(db_session, uuid.uuid4())

    rows = await _news_rows(db_session, asset_id=asset_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "gdelt"
    assert rows[0]["url"] == "https://news.example.com/zorb"


async def test_gdelt_not_queried_for_asset_with_native_feed(db_session: AsyncSession) -> None:
    await _hold_asset(db_session, symbol="WETH", name="Wrapped Ether")
    await _insert_feed(
        db_session,
        key="weth-feed",
        tier="project",
        asset_symbol="WETH",
        url="https://example.com/weth.xml",
    )

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/weth.xml").mock(
            return_value=Response(200, content=_atom("e1", "Works", "https://example.com/1"))
        )
        gdelt_route = mock.get("https://api.gdeltproject.org/api/v2/doc/doc").mock(
            return_value=Response(200, json={"articles": []})
        )
        await handle_news_refresh(db_session, uuid.uuid4())

    assert not gdelt_route.called


async def test_prune_removes_rows_older_than_90_days(db_session: AsyncSession) -> None:
    asset_id = await _insert_asset(db_session, symbol="OLD", name="Old Token")
    await db_session.execute(
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
    await db_session.flush()

    await handle_news_refresh(db_session, uuid.uuid4())

    rows = await _news_rows(db_session, asset_id=asset_id)
    assert [r["external_id"] for r in rows] == ["boundary"]
