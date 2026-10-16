"""Integration tests for GET/POST /api/v1/alerts (ADR-007 phase 1, AUD-583)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
    test_secret_key: str,
) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM news_alert"))
            await session.execute(text("DELETE FROM asset_news"))
            await session.execute(text("DELETE FROM asset"))
            await session.execute(text("DELETE FROM session"))
            await session.execute(text("DELETE FROM login_attempt"))
            await session.execute(text("DELETE FROM owner"))


async def _seed_asset(
    db_session_factory: async_sessionmaker[AsyncSession], *, symbol: str = "WETH"
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


async def _seed_alert(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_id: str,
    category: str = "hack_exploit",
    severity: str = "critical",
    summary: str = "heuristic match: 'hacked'",
    read_at: str | None = None,
    dispatched_via: str | None = None,
) -> str:
    alert_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            article_id = str(uuid.uuid4())
            await session.execute(
                text(
                    """
                    INSERT INTO asset_news
                      (id, asset_id, source, external_id, title, url, news_site, published_at)
                    VALUES
                      (:id, :asset_id, 'test', :ext, 'Protocol hacked', 'https://example.com',
                       'Example', now())
                    """
                ),
                {"id": article_id, "asset_id": asset_id, "ext": article_id},
            )
            await session.execute(
                text(
                    """
                    INSERT INTO news_alert
                      (id, asset_news_id, asset_id, category, severity, summary,
                       classifier, read_at, dispatched_at, dispatched_via)
                    VALUES
                      (:id, :article_id, :asset_id, :category, :severity, :summary,
                       'heuristic', :read_at, :dispatched_at, :dispatched_via)
                    """
                ),
                {
                    "id": alert_id,
                    "article_id": article_id,
                    "asset_id": asset_id,
                    "category": category,
                    "severity": severity,
                    "summary": summary,
                    "read_at": read_at,
                    # A road only ever exists on a dispatched row, so the two
                    # move together — same as the dispatcher's own UPDATE.
                    "dispatched_at": None if dispatched_via is None else "2026-10-16T12:00:00Z",
                    "dispatched_via": dispatched_via,
                },
            )
    return alert_id


async def test_lists_unread_before_read(
    seeded_client, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    await _seed_alert(db_session_factory, asset_id=asset_id, read_at="2026-01-01T00:00:00Z")
    unread_id = await _seed_alert(db_session_factory, asset_id=asset_id)

    r = await client.get("/api/v1/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["unread_count"] == 1
    assert body["alerts"][0]["id"] == unread_id
    assert body["alerts"][0]["read_at"] is None


async def test_the_delivery_road_is_visible_in_the_list(
    seeded_client, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A spooled alert must not read as a push at a human (AUD-595).

    Both roads set `dispatched_at` — spool->drain is a real delivery path, so
    the row has to leave the dispatcher's queue either way — and this endpoint
    is where someone looking at the app can tell which happened.
    """
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    spooled = await _seed_alert(db_session_factory, asset_id=asset_id, dispatched_via="spool")
    pushed = await _seed_alert(db_session_factory, asset_id=asset_id, dispatched_via="board")
    never = await _seed_alert(db_session_factory, asset_id=asset_id)

    r = await client.get("/api/v1/alerts")
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()["alerts"]}

    assert by_id[spooled]["dispatched_via"] == "spool"
    assert by_id[spooled]["dispatched_at"] is not None
    assert by_id[pushed]["dispatched_via"] == "board"
    # Never dispatched: no road, and no timestamp to imply one.
    assert by_id[never]["dispatched_via"] is None
    assert by_id[never]["dispatched_at"] is None


async def test_the_road_column_rejects_an_invented_value(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ck_news_alert_dispatched_via (migration 0034) is the backstop behind
    # alerts_mark's argparse `choices`: the vocabulary is fixed in one place and
    # anything outside it must not reach the table at all.
    asset_id = await _seed_asset(db_session_factory)
    with pytest.raises(IntegrityError):
        await _seed_alert(db_session_factory, asset_id=asset_id, dispatched_via="carrier-pigeon")


async def test_unread_filter(
    seeded_client, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    await _seed_alert(db_session_factory, asset_id=asset_id, read_at="2026-01-01T00:00:00Z")
    unread_id = await _seed_alert(db_session_factory, asset_id=asset_id)

    r = await client.get("/api/v1/alerts", params={"unread": True})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert [a["id"] for a in body["alerts"]] == [unread_id]


async def test_mark_read_is_idempotent(
    seeded_client, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    alert_id = await _seed_alert(db_session_factory, asset_id=asset_id)

    r1 = await client.post(f"/api/v1/alerts/{alert_id}/read", headers={"x-csrf-token": csrf})
    assert r1.status_code == 200
    first_read_at = r1.json()["read_at"]

    r2 = await client.post(f"/api/v1/alerts/{alert_id}/read", headers={"x-csrf-token": csrf})
    assert r2.status_code == 200
    assert r2.json()["read_at"] == first_read_at

    listing = await client.get("/api/v1/alerts")
    assert listing.json()["unread_count"] == 0


async def test_mark_read_requires_csrf(
    seeded_client, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client, _csrf = seeded_client
    asset_id = await _seed_asset(db_session_factory)
    alert_id = await _seed_alert(db_session_factory, asset_id=asset_id)

    r = await client.post(f"/api/v1/alerts/{alert_id}/read")
    assert r.status_code == 403


async def test_mark_read_unknown_alert_returns_404(seeded_client) -> None:
    client, csrf = seeded_client
    r = await client.post(f"/api/v1/alerts/{uuid.uuid4()}/read", headers={"x-csrf-token": csrf})
    assert r.status_code == 404


async def test_requires_session(seeded_client) -> None:
    client, _csrf = seeded_client
    client.cookies.clear()
    r = await client.get("/api/v1/alerts")
    assert r.status_code == 401
