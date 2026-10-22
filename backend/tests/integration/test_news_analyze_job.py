"""Integration tests for the news_analyze job (ADR-007 phase 1, AUD-583).

Covers the two acceptance criteria that matter most for this phase: a
cooldown window collapsing a wave of articles about one incident into a
single alert, and a neutral headline never creating an alert at all.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.jobs.news_analyze import handle_news_analyze

pytestmark = pytest.mark.integration


async def _purge(session: AsyncSession) -> None:
    await session.execute(text("DELETE FROM news_alert"))
    await session.execute(text("DELETE FROM asset_news"))
    await session.execute(text("DELETE FROM asset"))


@pytest.fixture()
async def analyze_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """A session with real commit/rollback — `handle_news_analyze` commits
    internally, same reason `test_news_refresh_job.py` bypasses `db_session`.
    """
    async with db_session_factory() as session:
        await _purge(session)
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()
            await _purge(session)
            await session.commit()


async def _insert_asset(session: AsyncSession, *, symbol: str = "WETH") -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source)
            VALUES (:id, :addr, :sym, :name, 18, 'manual')
            """
        ),
        {
            "id": asset_id,
            "addr": "0x" + uuid.uuid4().hex[:40],
            "sym": symbol,
            "name": f"{symbol} Token",
        },
    )
    return asset_id


async def _insert_article(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    title: str,
    published_at: datetime | None = None,
) -> uuid.UUID:
    article_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset_news
              (id, asset_id, source, external_id, title, url, news_site, published_at)
            VALUES
              (:id, :asset_id, 'test', :ext, :title, 'https://example.com/a', 'Example', :pub)
            """
        ),
        {
            "id": article_id,
            "asset_id": asset_id,
            "ext": str(article_id),
            "title": title,
            "pub": published_at or datetime.now(UTC),
        },
    )
    return article_id


async def _alert_rows(session: AsyncSession) -> list[tuple]:
    rows = await session.execute(
        text("SELECT asset_id, category, severity, summary FROM news_alert ORDER BY created_at")
    )
    return rows.all()


async def test_neutral_headline_creates_no_alert(analyze_session: AsyncSession) -> None:
    asset_id = await _insert_asset(analyze_session)
    article_id = await _insert_article(
        analyze_session, asset_id=asset_id, title="Project ships quarterly roadmap update"
    )
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    assert await _alert_rows(analyze_session) == []
    analyzed_at = (
        await analyze_session.execute(
            text("SELECT analyzed_at FROM asset_news WHERE id = :id"), {"id": article_id}
        )
    ).scalar_one()
    assert analyzed_at is not None


async def test_cooldown_collapses_wave_within_one_run(analyze_session: AsyncSession) -> None:
    """Two relevant headlines for one asset/category within 24h -> one alert."""
    asset_id = await _insert_asset(analyze_session)
    now = datetime.now(UTC)
    await _insert_article(
        analyze_session,
        asset_id=asset_id,
        title="Protocol hacked for $5M via flash loan",
        published_at=now,
    )
    await _insert_article(
        analyze_session,
        asset_id=asset_id,
        title="Follow-up: hacker drains more funds from protocol",
        published_at=now + timedelta(minutes=5),
    )
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    rows = await _alert_rows(analyze_session)
    assert len(rows) == 1
    assert rows[0][0] == asset_id
    assert rows[0][1] == "hack_exploit"
    assert rows[0][2] == "critical"


async def test_cooldown_collapses_wave_across_runs(analyze_session: AsyncSession) -> None:
    """Cooldown persists across runs, not just within one batch."""
    asset_id = await _insert_asset(analyze_session)
    await _insert_article(analyze_session, asset_id=asset_id, title="Exchange hacked for millions")
    await analyze_session.commit()
    await handle_news_analyze(analyze_session, uuid.uuid4())
    assert len(await _alert_rows(analyze_session)) == 1

    await _insert_article(
        analyze_session, asset_id=asset_id, title="Hacker moves stolen funds to mixer"
    )
    await analyze_session.commit()
    await handle_news_analyze(analyze_session, uuid.uuid4())

    assert len(await _alert_rows(analyze_session)) == 1


async def test_different_categories_both_alert(analyze_session: AsyncSession) -> None:
    """Cooldown is scoped per (asset, category) — a different category for the
    same asset must still alert."""
    asset_id = await _insert_asset(analyze_session)
    await _insert_article(analyze_session, asset_id=asset_id, title="Protocol hacked overnight")
    await _insert_article(
        analyze_session, asset_id=asset_id, title="Token price crashes 40% in panic selloff"
    )
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    rows = await _alert_rows(analyze_session)
    categories = {row[1] for row in rows}
    assert categories == {"hack_exploit", "market_move"}


async def test_different_assets_both_alert(analyze_session: AsyncSession) -> None:
    """Cooldown is scoped per asset — the same category for a different asset
    must still alert."""
    asset_a = await _insert_asset(analyze_session, symbol="WETH")
    asset_b = await _insert_asset(analyze_session, symbol="UNI")
    await _insert_article(analyze_session, asset_id=asset_a, title="WETH bridge hacked")
    await _insert_article(analyze_session, asset_id=asset_b, title="UNI pool exploited")
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    rows = await _alert_rows(analyze_session)
    assert {row[0] for row in rows} == {asset_a, asset_b}


async def test_one_story_matched_to_two_assets_alerts_once(
    analyze_session: AsyncSession,
) -> None:
    """The headline the owner reads is the same — so it must alert once.

    The `(asset_id, category)` cooldown cannot catch this: a story naming two
    held tokens matches two assets, clears two separate cooldowns, and used to
    land as two alerts the owner read as the same news repeated (AUD-683).
    """
    asset_a = await _insert_asset(analyze_session, symbol="WETH")
    asset_b = await _insert_asset(analyze_session, symbol="UNI")
    title = "Bridge exploited for $40M, WETH and UNI drained"
    await _insert_article(analyze_session, asset_id=asset_a, title=title)
    await _insert_article(analyze_session, asset_id=asset_b, title=title)
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    assert len(await _alert_rows(analyze_session)) == 1


async def test_the_same_story_under_two_publishers_alerts_once(
    analyze_session: AsyncSession,
) -> None:
    """Feeds append " - Publisher", so one wire story arrives spelled N ways."""
    asset_a = await _insert_asset(analyze_session, symbol="WETH")
    asset_b = await _insert_asset(analyze_session, symbol="UNI")
    await _insert_article(
        analyze_session, asset_id=asset_a, title="Bridge exploited for $40M - CoinDesk"
    )
    await _insert_article(
        analyze_session, asset_id=asset_b, title="Bridge exploited for $40M - Decrypt"
    )
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    assert len(await _alert_rows(analyze_session)) == 1


async def test_already_analyzed_article_is_skipped(analyze_session: AsyncSession) -> None:
    asset_id = await _insert_asset(analyze_session)
    article_id = await _insert_article(
        analyze_session, asset_id=asset_id, title="Protocol hacked for $1M"
    )
    await analyze_session.execute(
        text("UPDATE asset_news SET analyzed_at = now() WHERE id = :id"), {"id": article_id}
    )
    await analyze_session.commit()

    await handle_news_analyze(analyze_session, uuid.uuid4())

    assert await _alert_rows(analyze_session) == []
