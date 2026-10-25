"""Integration tests for GET /api/v1/assets/{asset_id}/news (AUD-308)."""

from __future__ import annotations

import uuid

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
            await session.execute(text("DELETE FROM asset_news"))
            await session.execute(text("DELETE FROM asset"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM owner"))


async def _seed_asset(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    symbol: str = "WETH",
) -> str:
    asset_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
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


async def _seed_news(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_id: str,
    external_id: str,
    title: str,
    published_at: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO asset_news
                      (asset_id, source, external_id, title, url, news_site, published_at)
                    VALUES
                      (:asset_id, 'coingecko', :external_id, :title,
                       'https://example.com/' || :external_id, 'Example', :published_at)
                    """
                ),
                {
                    "asset_id": asset_id,
                    "external_id": external_id,
                    "title": title,
                    "published_at": published_at,
                },
            )


async def test_returns_news_newest_first(
    seeded_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    await _seed_news(
        db_session_factory,
        asset_id=asset_id,
        external_id="older",
        title="Older article",
        published_at="2026-01-01T00:00:00Z",
    )
    await _seed_news(
        db_session_factory,
        asset_id=asset_id,
        external_id="newer",
        title="Newer article",
        published_at="2026-06-01T00:00:00Z",
    )

    r = await client.get(f"/api/v1/assets/{asset_id}/news")
    assert r.status_code == 200
    body = r.json()
    assert body["asset_id"] == asset_id
    assert body["total"] == 2
    assert [item["title"] for item in body["news"]] == ["Newer article", "Older article"]


async def test_only_returns_news_for_requested_asset(
    seeded_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    asset_a = await _seed_asset(db_session_factory, symbol="WETH")
    asset_b = await _seed_asset(db_session_factory, symbol="UNI")
    await _seed_news(
        db_session_factory,
        asset_id=asset_a,
        external_id="a1",
        title="WETH news",
        published_at="2026-01-01T00:00:00Z",
    )
    await _seed_news(
        db_session_factory,
        asset_id=asset_b,
        external_id="b1",
        title="UNI news",
        published_at="2026-01-01T00:00:00Z",
    )

    r = await client.get(f"/api/v1/assets/{asset_a}/news")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["news"][0]["title"] == "WETH news"


async def test_pagination(
    seeded_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    for i in range(3):
        await _seed_news(
            db_session_factory,
            asset_id=asset_id,
            external_id=f"n{i}",
            title=f"Article {i}",
            published_at=f"2026-01-0{i + 1}T00:00:00Z",
        )

    r = await client.get(f"/api/v1/assets/{asset_id}/news", params={"limit": 1, "offset": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert len(body["news"]) == 1
    # offset 1, newest-first -> the middle article by published_at
    assert body["news"][0]["title"] == "Article 1"


async def test_unknown_asset_returns_404(seeded_client) -> None:
    client, _csrf = seeded_client
    r = await client.get(f"/api/v1/assets/{uuid.uuid4()}/news")
    assert r.status_code == 404


async def test_invalid_asset_id_returns_400(seeded_client) -> None:
    client, _csrf = seeded_client
    r = await client.get("/api/v1/assets/not-a-uuid/news")
    assert r.status_code == 400


async def test_requires_session(
    seeded_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    unauth = client
    unauth.cookies.clear()
    r = await unauth.get(f"/api/v1/assets/{asset_id}/news")
    assert r.status_code == 401


# --- LLM significance gate fields (AUD-658 writes them, AUD-743 exposes them) ---


async def _seed_analyzed_news(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_id: str,
    external_id: str,
    summary: str,
    importance: int,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO asset_news
                      (asset_id, source, external_id, title, url, news_site,
                       published_at, summary, importance)
                    VALUES
                      (:asset_id, 'gdelt', :external_id, 'Analyzed article',
                       'https://example.com/' || :external_id, 'Example',
                       '2026-05-01T00:00:00Z', :summary, :importance)
                    """
                ),
                {
                    "asset_id": asset_id,
                    "external_id": external_id,
                    "summary": summary,
                    "importance": importance,
                },
            )


async def test_returns_summary_and_importance_when_set(
    seeded_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    await _seed_analyzed_news(
        db_session_factory,
        asset_id=asset_id,
        external_id="analyzed",
        summary="The protocol was exploited for $10M.",
        importance=85,
    )

    r = await client.get(f"/api/v1/assets/{asset_id}/news")
    assert r.status_code == 200
    item = r.json()["news"][0]
    assert item["summary"] == "The protocol was exploited for $10M."
    assert item["importance"] == 85


async def test_summary_and_importance_are_null_for_unanalyzed_news(
    seeded_client,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Gate off (or failed open) means no summary — the fields must still be
    present and null rather than missing, so the UI renders neither."""
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    await _seed_news(
        db_session_factory,
        asset_id=asset_id,
        external_id="plain",
        title="Plain article",
        published_at="2026-01-01T00:00:00Z",
    )

    r = await client.get(f"/api/v1/assets/{asset_id}/news")
    assert r.status_code == 200
    item = r.json()["news"][0]
    assert item["summary"] is None
    assert item["importance"] is None
