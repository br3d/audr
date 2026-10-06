"""AUD-447: Exclude must take effect on the next read, not on the next snapshot.

GET /portfolio renders the latest *published* valuation_snapshot. Excluding an
asset only flips `asset.excluded`, which publish_valuation_snapshot honours the
next time it runs — so the dashboard total, the allocation table and the
percentages all kept counting an asset the owner had just excluded until the
hourly valuation happened to republish. The exclusion is now applied at read
time against the asset's current flag.

Second half of the same report: the excluded asset also has to leave the Assets
list (it is recoverable through the "Show excluded" filter), and the hidden
count has to survive a page that deliberately contains none of those rows.

AUD-448 finished the job. Exclusion is now purely a read-time concern:
snapshots record every held line, GET /portfolio returns the excluded ones
flagged `included: false` with their value intact, and GET /history re-cuts
every past point against the current exclusion set. That is what lets the
client redraw on the click itself, in both directions — putting an asset back
needs no re-scan to recover a number that was never thrown away.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_PORTFOLIO_URL = "/api/v1/portfolio"
_ASSETS_URL = "/api/v1/assets"
_PASSWORD = "correct-horse-battery-staple-99"


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


_CLEAN_ORDER = (
    "history_point",
    "valuation_line",
    "valuation_snapshot",
    "balance_observation",
    "monitored_pair",
    "asset",
    "wallet",
    "login_attempt",
    "session",
    "owner",
)


async def _wipe(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with db_session_factory() as session:
        async with session.begin():
            for table in _CLEAN_ORDER:
                await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 -- table is from the fixed _CLEAN_ORDER tuple


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    # These routes commit, so rows left behind would outlive the module.
    await _wipe(db_session_factory)
    yield
    await _wipe(db_session_factory)


@pytest.fixture()
async def http_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[httpx.AsyncClient]:
    app.dependency_overrides[get_db] = _make_override(db_session_factory)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=_BASE) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _setup_and_get_csrf(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


async def _seed_wallet(
    factory: async_sessionmaker[AsyncSession], address: str = "0x" + "a" * 40
) -> str:
    wallet_id = str(uuid.uuid4())
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO wallet (id, address, label, status)"
                    " VALUES (:id, :addr, '', 'active')"
                ),
                {"id": wallet_id, "addr": address.lower()},
            )
    return wallet_id


async def _seed_asset(
    factory: async_sessionmaker[AsyncSession], *, token_address: str, symbol: str
) -> str:
    asset_id = str(uuid.uuid4())
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                    " VALUES (:id, :addr, :sym, :sym, 18, 'catalog')"
                ),
                {"id": asset_id, "addr": token_address.lower(), "sym": symbol},
            )
    return asset_id


async def _seed_snapshot(factory: async_sessionmaker[AsyncSession], *, holdings: list[dict]) -> str:
    """Publish a snapshot carrying the given lines, as the valuation job would."""
    snap_id = str(uuid.uuid4())
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO valuation_snapshot"
                    " (id, snapshotted_at, quality, published_at, input_key)"
                    " VALUES (:id, NOW(), 'complete', NOW(), :input_key)"
                ),
                {"id": snap_id, "input_key": snap_id},
            )
            for h in holdings:
                await session.execute(
                    text(
                        "INSERT INTO valuation_line"
                        " (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,"
                        " block_time, price_usd, value_usd)"
                        " VALUES (:id, :snap, :wallet, :asset, :raw, 12345678, NOW(),"
                        " :price, :value)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "snap": snap_id,
                        "wallet": h["wallet_id"],
                        "asset": h["asset_id"],
                        "raw": h.get("raw_amount", "1000000000000000000"),
                        "price": h["price_usd"],
                        "value": h["value_usd"],
                    },
                )
    return snap_id


async def _seed_balance(
    factory: async_sessionmaker[AsyncSession], *, wallet_id: str, asset_id: str
) -> None:
    """A balance observation is what makes an asset `held` for the list filter."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO balance_observation"
                    " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
                    " VALUES (:id, :wallet, :asset, 1000000000000000000, 12345678, NOW())"
                ),
                {"id": str(uuid.uuid4()), "wallet": wallet_id, "asset": asset_id},
            )


@pytest.mark.integration
async def test_exclude_drops_the_asset_from_the_portfolio_without_a_new_snapshot(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Total, allocations and percentages recompute on the very next read."""
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    keep_id = await _seed_asset(db_session_factory, token_address="0x" + "b" * 40, symbol="KEEP")
    drop_id = await _seed_asset(db_session_factory, token_address="0x" + "c" * 40, symbol="DROP")
    snap_id = await _seed_snapshot(
        db_session_factory,
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": keep_id,
                "price_usd": "100",
                "value_usd": "100",
            },
            {
                "wallet_id": wallet_id,
                "asset_id": drop_id,
                "price_usd": "300",
                "value_usd": "300",
            },
        ],
    )

    before = (await http_client.get(_PORTFOLIO_URL)).json()
    assert Decimal(before["total_usd"]) == Decimal(400)
    assert {a["symbol"] for a in before["allocations"]} == {"KEEP", "DROP"}

    r = await http_client.patch(
        f"{_ASSETS_URL}/{drop_id}",
        json={"excluded": True},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    after = (await http_client.get(_PORTFOLIO_URL)).json()

    # Same snapshot — no valuation run happened in between.
    assert after["snapshot_id"] == snap_id
    assert Decimal(after["total_usd"]) == Decimal(100)
    assert Decimal(after["priced_subtotal_usd"]) == Decimal(100)
    # The excluded asset is still returned, flagged and at a 0% share, so the
    # client can put it back without a round trip or a re-scan (AUD-448).
    by_symbol = {a["symbol"]: a for a in after["allocations"]}
    assert set(by_symbol) == {"KEEP", "DROP"}
    assert by_symbol["DROP"]["included"] is False
    assert Decimal(by_symbol["DROP"]["percentage"]) == Decimal(0)
    assert Decimal(by_symbol["DROP"]["value_usd"]) == Decimal(300)

    # The remaining asset is now the whole portfolio, not a quarter of it.
    assert by_symbol["KEEP"]["included"] is True
    assert Decimal(by_symbol["KEEP"]["percentage"]) == Decimal(100)

    included_holdings = [h["asset_id"] for h in after["holdings"] if h["included"]]
    assert included_holdings == [keep_id]

    # ...and including it again restores the total, from that same snapshot.
    r = await http_client.patch(
        f"{_ASSETS_URL}/{drop_id}",
        json={"excluded": False},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    restored = (await http_client.get(_PORTFOLIO_URL)).json()
    assert Decimal(restored["total_usd"]) == Decimal(400)


async def _seed_history_point(
    factory: async_sessionmaker[AsyncSession], *, snapshot_id: str, total_value_usd: str
) -> None:
    """A history_point carrying the snapshot's *gross* total, as the worker writes it."""
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO history_point"
                    " (id, snapshot_id, snapshotted_at, total_value_usd, quality,"
                    "  included_wallet_count, included_asset_count, has_gap, is_canonical)"
                    " SELECT :id, :snap, vs.snapshotted_at, :total, 'complete', 1, 2, false, true"
                    " FROM valuation_snapshot vs WHERE vs.id = :snap"
                ),
                {"id": str(uuid.uuid4()), "snap": snapshot_id, "total": total_value_usd},
            )


@pytest.mark.integration
async def test_exclude_redraws_the_history_chart_on_the_next_read(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The chart must follow the current exclusion set, not the snapshot's (AUD-448).

    Without this the curve and the headline total describe the same instant
    differently, and the step in the line records when the owner pressed a
    button rather than anything the portfolio did.
    """
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    keep_id = await _seed_asset(db_session_factory, token_address="0x" + "1" * 40, symbol="KEEP")
    drop_id = await _seed_asset(db_session_factory, token_address="0x" + "2" * 40, symbol="DROP")
    snap_id = await _seed_snapshot(
        db_session_factory,
        holdings=[
            {"wallet_id": wallet_id, "asset_id": keep_id, "price_usd": "100", "value_usd": "100"},
            {"wallet_id": wallet_id, "asset_id": drop_id, "price_usd": "300", "value_usd": "300"},
        ],
    )
    await _seed_history_point(db_session_factory, snapshot_id=snap_id, total_value_usd="400")

    before = (await http_client.get("/api/v1/history?period=all")).json()
    assert [Decimal(i["total_value_usd"]) for i in before["items"]] == [Decimal(400)]

    r = await http_client.patch(
        f"{_ASSETS_URL}/{drop_id}",
        json={"excluded": True},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    after = (await http_client.get("/api/v1/history?period=all")).json()
    # The past point is re-cut too: the excluded asset was never the owner's.
    assert [Decimal(i["total_value_usd"]) for i in after["items"]] == [Decimal(100)]
    assert after["items"][0]["included_asset_count"] == 1

    # The chart agrees with the headline total at the same instant.
    portfolio = (await http_client.get(_PORTFOLIO_URL)).json()
    assert Decimal(portfolio["total_usd"]) == Decimal(100)

    # Including it again restores the curve — nothing was destroyed.
    r = await http_client.patch(
        f"{_ASSETS_URL}/{drop_id}",
        json={"excluded": False},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200
    restored = (await http_client.get("/api/v1/history?period=all")).json()
    assert [Decimal(i["total_value_usd"]) for i in restored["items"]] == [Decimal(400)]


@pytest.mark.integration
async def test_excluded_asset_leaves_the_default_assets_list_but_stays_reachable(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """excluded_count must not depend on the excluded rows being on the page."""
    csrf = await _setup_and_get_csrf(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    keep_id = await _seed_asset(db_session_factory, token_address="0x" + "d" * 40, symbol="KEEP")
    drop_id = await _seed_asset(db_session_factory, token_address="0x" + "e" * 40, symbol="DROP")
    await _seed_balance(db_session_factory, wallet_id=wallet_id, asset_id=keep_id)
    await _seed_balance(db_session_factory, wallet_id=wallet_id, asset_id=drop_id)

    r = await http_client.patch(
        f"{_ASSETS_URL}/{drop_id}",
        json={"excluded": True},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    # The default view the UI asks for: held, not excluded.
    listed = (await http_client.get(f"{_ASSETS_URL}?held=true&excluded=false")).json()
    assert [a["id"] for a in listed["items"]] == [keep_id]
    assert listed["excluded_count"] == 1

    # The "Show excluded" filter is the way back.
    hidden = (await http_client.get(f"{_ASSETS_URL}?held=true&excluded=true")).json()
    assert [a["id"] for a in hidden["items"]] == [drop_id]
    assert hidden["excluded_count"] == 1
