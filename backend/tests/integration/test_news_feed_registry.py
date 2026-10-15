"""Integration tests for the news feed registry DB access (AUD-512)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.news_feed_registry import (
    covered_asset_symbols,
    list_due_feeds,
    record_feed_failure,
    record_feed_not_modified,
    record_feed_success,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_news_feed(db_session: AsyncSession) -> None:
    await db_session.execute(text("DELETE FROM news_feed"))


async def _insert_feed(
    db_session: AsyncSession,
    *,
    key: str,
    tier: str = "press",
    symbols: list[str] | None = None,
    url: str = "https://example.com/feed.xml",
    enabled: bool = True,
    last_polled_at_sql: str = "NULL",
    etag: str | None = None,
    last_modified: str | None = None,
) -> uuid.UUID:
    feed_id = uuid.uuid4()
    await db_session.execute(
        text(
            f"""
            INSERT INTO news_feed
              (id, key, tier, url, enabled, last_polled_at, etag, last_modified)
            VALUES
              (:id, :key, :tier, :url, :enabled, {last_polled_at_sql}, :etag, :lm)
            """  # noqa: S608 — last_polled_at_sql is a module-local constant, not user input
        ),
        {
            "id": str(feed_id),
            "key": key,
            "tier": tier,
            "url": url,
            "enabled": enabled,
            "etag": etag,
            "lm": last_modified,
        },
    )
    for symbol in symbols or []:
        await db_session.execute(
            text("INSERT INTO news_feed_symbol (feed_id, asset_symbol) VALUES (:feed_id, :symbol)"),
            {"feed_id": str(feed_id), "symbol": symbol},
        )
    await db_session.flush()
    return feed_id


async def test_list_due_feeds_includes_never_polled(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="never-polled")
    due = await list_due_feeds(db_session)
    assert [f.key for f in due] == ["never-polled"]


async def test_list_due_feeds_excludes_recently_polled(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="recent", last_polled_at_sql="now() - interval '1 minute'")
    due = await list_due_feeds(db_session)
    assert due == []


async def test_list_due_feeds_includes_stale_poll(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="stale", last_polled_at_sql="now() - interval '1 day'")
    due = await list_due_feeds(db_session)
    assert [f.key for f in due] == ["stale"]


async def test_list_due_feeds_excludes_disabled(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="off", enabled=False)
    due = await list_due_feeds(db_session)
    assert due == []


async def test_list_due_feeds_symbols_empty_for_press_feed(db_session: AsyncSession) -> None:
    """No rows in `news_feed_symbol` for a feed means press tier, allow-list
    matched — distinct from a feed someone mapped explicitly (AUD-571).
    """
    await _insert_feed(db_session, key="press:x", tier="press")
    due = await list_due_feeds(db_session)
    assert due[0].symbols == frozenset()


async def test_list_due_feeds_symbols_for_multi_symbol_feed(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="eth-feed", tier="project", symbols=["WETH", "ETH"])
    due = await list_due_feeds(db_session)
    assert due[0].symbols == frozenset({"WETH", "ETH"})


async def test_list_due_feeds_carries_validators(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="validated", etag='"abc"', last_modified="Mon, 01 Jan 2026")
    due = await list_due_feeds(db_session)
    assert due[0].etag == '"abc"'
    assert due[0].last_modified == "Mon, 01 Jan 2026"


async def test_record_feed_success_updates_validators_and_resets_failures(
    db_session: AsyncSession,
) -> None:
    feed_id = await _insert_feed(db_session, key="k")
    await record_feed_failure(db_session, feed_id=feed_id, error="boom")
    await record_feed_success(db_session, feed_id=feed_id, etag='"v2"', last_modified="Tue")

    row = (
        await db_session.execute(
            text(
                "SELECT etag, last_modified, consecutive_failures, last_error, last_success_at"
                " FROM news_feed WHERE id = :id"
            ),
            {"id": str(feed_id)},
        )
    ).one()
    assert row[0] == '"v2"'
    assert row[1] == "Tue"
    assert row[2] == 0
    assert row[3] is None
    assert row[4] is not None


async def test_record_feed_failure_increments_streak_without_touching_success(
    db_session: AsyncSession,
) -> None:
    feed_id = await _insert_feed(db_session, key="k")
    await record_feed_failure(db_session, feed_id=feed_id, error="HTTP 404")
    await record_feed_failure(db_session, feed_id=feed_id, error="HTTP 404 again")

    row = (
        await db_session.execute(
            text(
                "SELECT consecutive_failures, last_error, last_success_at"
                " FROM news_feed WHERE id = :id"
            ),
            {"id": str(feed_id)},
        )
    ).one()
    assert row[0] == 2
    assert row[1] == "HTTP 404 again"
    assert row[2] is None


async def test_record_feed_not_modified_is_success(db_session: AsyncSession) -> None:
    feed_id = await _insert_feed(db_session, key="k")
    await record_feed_failure(db_session, feed_id=feed_id, error="boom")
    await record_feed_not_modified(db_session, feed_id=feed_id)

    row = (
        await db_session.execute(
            text(
                "SELECT consecutive_failures, last_error, last_success_at"
                " FROM news_feed WHERE id = :id"
            ),
            {"id": str(feed_id)},
        )
    ).one()
    assert row[0] == 0
    assert row[1] is None
    assert row[2] is not None


async def test_covered_asset_symbols_includes_project_and_release(
    db_session: AsyncSession,
) -> None:
    await _insert_feed(db_session, key="p", tier="project", symbols=["WETH"])
    await _insert_feed(db_session, key="r", tier="release", symbols=["UNI"])
    await _insert_feed(db_session, key="press", tier="press")
    assert await covered_asset_symbols(db_session) == {"WETH", "UNI"}


async def test_covered_asset_symbols_excludes_disabled(db_session: AsyncSession) -> None:
    await _insert_feed(db_session, key="p", tier="project", symbols=["WETH"], enabled=False)
    assert await covered_asset_symbols(db_session) == set()
