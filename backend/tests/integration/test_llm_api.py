"""Integration tests for the LLM read surface (AUD-630).

AC coverage:
- Both routes require session or read-token auth.
- Every response carries as_of and caveats.
- An unpriced asset never becomes zero.
- /asset/{ref} resolves by symbol, by contract address, and by UUID.
- Accept: text/markdown is honoured alongside JSON.
- The /context response size budget is fixed by a test.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from audr.api.app import app
from audr.db import get_db
from audr.operations.init_key import init_key
from tests.helpers import wallet_address_columns

pytestmark = pytest.mark.integration

_BASE = "http://test"
_SETUP_URL = "/api/v1/setup"
_PASSWORD = "correct-horse-battery-staple-99"
_CONTEXT_URL = "/api/v1/llm/context"

# Keep response bodies under roughly this many characters, a stand-in for the
# ~2k-token budget docs/llm-access.md sets for GET /context (~4 chars/token).
_CONTEXT_CHAR_BUDGET = 8000

_CLEAN_ORDER = (
    "news_alert",
    "asset_news",
    "history_point",
    "valuation_line",
    "valuation_snapshot",
    "quote_observation",
    "quote_set",
    "token_attempt",
    "api_token",
    "wallet",
    "asset",
    "login_attempt",
    "session",
    "owner",
    "key_state",
)


@pytest.fixture(autouse=True)
async def _clean_tables(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    async def _wipe() -> None:
        async with db_session_factory() as session:
            async with session.begin():
                for table in _CLEAN_ORDER:
                    await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 -- table is from the fixed _CLEAN_ORDER tuple
            async with session.begin():
                await init_key(session)

    await _wipe()
    yield
    await _wipe()


def _make_override(factory: async_sessionmaker[AsyncSession]):
    async def _override() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            yield session

    return _override


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


async def _setup_and_login(client: httpx.AsyncClient) -> str:
    r = await client.post(_SETUP_URL, json={"password": _PASSWORD})
    assert r.status_code == 201
    return r.json()["csrf_token"]


async def _mint_token_via_api(client: httpx.AsyncClient, csrf: str) -> str:
    r = await client.post(
        "/api/v1/tokens",
        json={"name": "llm-test", "expires_days": 90, "redact_addresses": False},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 201
    return r.json()["token"]


async def _seed_wallet(
    db_session_factory: async_sessionmaker[AsyncSession],
    address: str = "0x" + "a" * 40,
) -> str:
    wallet_id = uuid.uuid4()
    async with db_session_factory() as session:
        async with session.begin():
            cols = await wallet_address_columns(session, wallet_id, address)
            await session.execute(
                text(
                    "INSERT INTO wallet"
                    " (id, address_ciphertext, address_bidx, label_ciphertext, status)"
                    " VALUES (:id, :addr_ct, :addr_bidx, '', 'active')"
                ),
                {
                    "id": str(wallet_id),
                    "addr_ct": cols["address_ciphertext"],
                    "addr_bidx": cols["address_bidx"],
                },
            )
    return str(wallet_id)


async def _seed_asset(
    db_session_factory: async_sessionmaker[AsyncSession],
    token_address: str,
    symbol: str = "TKN",
    decimals: int = 18,
    name: str | None = None,
) -> str:
    asset_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                    " VALUES (:id, :addr, :sym, :name, :dec, 'catalog')"
                ),
                {
                    "id": asset_id,
                    "addr": token_address.lower(),
                    "sym": symbol,
                    "name": name if name is not None else symbol,
                    "dec": decimals,
                },
            )
    return asset_id


async def _seed_snapshot(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    holdings: list[dict] | None = None,
    quality: str = "complete",
    snapshotted_at_sql: str = "now()",
) -> str:
    snap_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO valuation_snapshot"  # noqa: S608 -- snapshotted_at_sql is a fixed test-local constant
                    " (id, snapshotted_at, quality, published_at, input_key)"
                    f" VALUES (:id, {snapshotted_at_sql}, :quality, now(), :input_key)"
                ),
                {"id": snap_id, "quality": quality, "input_key": snap_id},
            )
            for h in holdings or []:
                await session.execute(
                    text(
                        "INSERT INTO valuation_line"
                        " (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,"
                        " price_usd, value_usd)"
                        " VALUES (:id, :snap, :wallet, :asset, :raw, :block, :price, :value)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "snap": snap_id,
                        "wallet": h["wallet_id"],
                        "asset": h["asset_id"],
                        "raw": h.get("raw_amount", "1000000000000000000"),
                        "block": h.get("block_number", 1),
                        "price": h.get("price_usd"),
                        "value": h.get("value_usd"),
                    },
                )
    return snap_id


async def _seed_history_point(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    total_value_usd: str | None,
    quality: str = "complete",
    snapshotted_at_sql: str,
) -> None:
    """Insert a standalone valuation_snapshot + history_point pair at a given time.

    *snapshotted_at_sql* is a raw SQL expression (e.g. "now() - interval '24 hours'"),
    test-local only, never user input.
    """
    snap_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            # published_at is backdated too (not `now()`), so this historical
            # snapshot never outranks the real holdings snapshot for "latest
            # published" purposes in get_portfolio().
            await session.execute(
                text(
                    "INSERT INTO valuation_snapshot"  # noqa: S608 -- snapshotted_at_sql is a fixed test-local constant
                    " (id, snapshotted_at, quality, published_at, input_key)"
                    f" VALUES (:id, {snapshotted_at_sql}, :quality,"
                    f" {snapshotted_at_sql}, :input_key)"
                ),
                {"id": snap_id, "quality": quality, "input_key": snap_id},
            )
            await session.execute(
                text(
                    "INSERT INTO history_point"  # noqa: S608 -- snapshotted_at_sql is a fixed test-local constant
                    " (id, snapshot_id, snapshotted_at, total_value_usd, quality,"
                    " included_wallet_count, included_asset_count)"
                    f" VALUES (:id, :snap, {snapshotted_at_sql}, :total, :quality, 1, 1)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "snap": snap_id,
                    "total": total_value_usd,
                    "quality": quality,
                },
            )


async def _seed_news(
    db_session_factory: async_sessionmaker[AsyncSession],
    asset_id: str,
    *,
    title: str = "Something happened",
    external_id: str | None = None,
) -> str:
    news_id = str(uuid.uuid4())
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO asset_news"
                    " (id, asset_id, source, external_id, title, url, news_site, published_at)"
                    " VALUES (:id, :asset, 'gdelt', :ext, :title, :url, 'example.com', now())"
                ),
                {
                    "id": news_id,
                    "asset": asset_id,
                    "ext": external_id or news_id,
                    "title": title,
                    "url": f"https://example.com/{news_id}",
                },
            )
    return news_id


async def _seed_alert(
    db_session_factory: async_sessionmaker[AsyncSession],
    asset_news_id: str,
    asset_id: str,
    *,
    severity: str = "warning",
    summary: str = "Something worth flagging",
    read: bool = False,
) -> None:
    read_clause = "now()" if read else "NULL"
    async with db_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO news_alert"  # noqa: S608 -- read_clause is one of two hardcoded literals, not user input
                    " (id, asset_news_id, asset_id, category, severity, summary, classifier,"
                    " read_at)"
                    f" VALUES (:id, :news, :asset, 'market_move', :severity, :summary,"
                    f" 'heuristic', {read_clause})"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "news": asset_news_id,
                    "asset": asset_id,
                    "severity": severity,
                    "summary": summary,
                },
            )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_context_requires_auth(http_client: httpx.AsyncClient) -> None:
    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 401


async def test_asset_requires_auth(http_client: httpx.AsyncClient) -> None:
    # Auth is checked before resolution, so an unauthenticated request never
    # reaches the resolver regardless of whether "ETH" would resolve.
    r = await http_client.get("/api/v1/llm/asset/ETH")
    assert r.status_code == 401


async def test_context_accessible_with_session(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)
    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200


async def test_context_accessible_with_token(http_client: httpx.AsyncClient) -> None:
    csrf = await _setup_and_login(http_client)
    token = await _mint_token_via_api(http_client, csrf)
    http_client.cookies.clear()
    r = await http_client.get(_CONTEXT_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_asset_accessible_with_token(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _setup_and_login(http_client)
    token = await _mint_token_via_api(http_client, csrf)
    asset_id = await _seed_asset(db_session_factory, "0x" + "c" * 40, symbol="FOO")
    http_client.cookies.clear()
    r = await http_client.get(
        f"/api/v1/llm/asset/{asset_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# as_of + caveats always present
# ---------------------------------------------------------------------------


async def test_context_has_as_of_and_caveats_with_no_data(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)
    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["as_of"]
    assert isinstance(body["caveats"], list)
    assert len(body["caveats"]) >= 1
    # No snapshot yet → every figure is explicitly unknown, not zero.
    assert body["portfolio"]["total_usd"] is None
    assert any("snapshot" in c.lower() for c in body["caveats"])


async def test_asset_has_as_of_and_caveats(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "d" * 40, symbol="BAR")
    r = await http_client.get(f"/api/v1/llm/asset/{asset_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["as_of"]
    assert isinstance(body["caveats"], list)
    assert len(body["caveats"]) >= 1
    # Never held → unknown, not zero.
    assert body["holding"] is None
    assert any("not currently held" in c for c in body["caveats"])


# ---------------------------------------------------------------------------
# Unpriced asset never becomes zero
# ---------------------------------------------------------------------------


async def test_unpriced_holding_leaves_total_null_not_zero(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory, "0x" + "e" * 40, symbol="UNP")
    await _seed_snapshot(
        db_session_factory,
        quality="partial",
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": asset_id,
                "raw_amount": "1000000000000000000",
                "price_usd": None,
                "value_usd": None,
            }
        ],
    )

    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["portfolio"]["total_usd"] is None
    assert body["portfolio"]["unpriced_asset_count"] == 1
    assert any("unpriced" in c for c in body["caveats"])

    r2 = await http_client.get(f"/api/v1/llm/asset/{asset_id}")
    assert r2.status_code == 200
    asset_body = r2.json()
    assert asset_body["holding"] is not None
    assert asset_body["holding"]["value_usd"] is None
    assert asset_body["holding"]["price_usd"] is None
    assert any("no price is available" in c.lower() for c in asset_body["caveats"])


# ---------------------------------------------------------------------------
# Context: priced holding, top allocation, deltas
# ---------------------------------------------------------------------------


async def test_context_reports_priced_total_and_allocation(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory, "0x" + "f" * 40, symbol="PRC")
    await _seed_snapshot(
        db_session_factory,
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": asset_id,
                "raw_amount": "2000000000000000000",
                "price_usd": "100.000000000000000000",
                "value_usd": "200.000000000000000000",
            }
        ],
    )

    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["portfolio"]["total_usd"] == "200.000000000000000000"
    assert len(body["top_allocations"]) == 1
    assert body["top_allocations"][0]["symbol"] == "PRC"
    assert body["top_allocations"][0]["value_usd"] == "200.000000000000000000"


async def test_context_24h_delta_from_history_point(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    wallet_id = await _seed_wallet(db_session_factory)
    asset_id = await _seed_asset(db_session_factory, "0x" + "1" * 40, symbol="DLT")
    await _seed_snapshot(
        db_session_factory,
        holdings=[
            {
                "wallet_id": wallet_id,
                "asset_id": asset_id,
                "raw_amount": "1000000000000000000",
                "price_usd": "110.000000000000000000",
                "value_usd": "110.000000000000000000",
            }
        ],
    )
    await _seed_history_point(
        db_session_factory,
        total_value_usd="100.000000000000000000",
        snapshotted_at_sql="now() - interval '24 hours'",
    )

    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["deltas"]["pct_24h"] == "10.00"


# ---------------------------------------------------------------------------
# /asset/{ref} resolution: symbol, address, UUID
# ---------------------------------------------------------------------------


async def test_asset_resolves_by_symbol_case_insensitive(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "2" * 40, symbol="ZOO")
    r = await http_client.get("/api/v1/llm/asset/zoo")
    assert r.status_code == 200
    assert r.json()["asset_id"] == asset_id


async def test_asset_resolves_by_contract_address(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    addr = "0x" + "ab" * 20
    asset_id = await _seed_asset(db_session_factory, addr, symbol="ADR")
    r = await http_client.get(f"/api/v1/llm/asset/{addr.upper()}")
    assert r.status_code == 200
    assert r.json()["asset_id"] == asset_id


async def test_asset_resolves_by_uuid(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "4" * 40, symbol="IDR")
    r = await http_client.get(f"/api/v1/llm/asset/{asset_id}")
    assert r.status_code == 200
    assert r.json()["symbol"] == "IDR"


async def test_asset_unknown_ref_returns_404(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)
    r = await http_client.get("/api/v1/llm/asset/NOPE")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# News and alerts
# ---------------------------------------------------------------------------


async def test_asset_includes_news_and_alerts(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "5" * 40, symbol="NWS")
    news_id = await _seed_news(db_session_factory, asset_id, title="Big headline")
    await _seed_alert(db_session_factory, news_id, asset_id, summary="Flagged thing")

    r = await http_client.get(f"/api/v1/llm/asset/{asset_id}")
    assert r.status_code == 200
    body = r.json()
    assert len(body["news"]) == 1
    assert body["news"][0]["title"] == "Big headline"
    assert body["alerts"]["unread_count"] == 1
    assert body["alerts"]["items"][0]["summary"] == "Flagged thing"


async def test_context_includes_headlines_and_alerts(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "6" * 40, symbol="CTX")
    news_id = await _seed_news(db_session_factory, asset_id, title="Context headline")
    await _seed_alert(db_session_factory, news_id, asset_id, summary="Context alert")

    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200
    body = r.json()
    assert any(h["title"] == "Context headline" for h in body["headlines"])
    assert body["alerts"]["unread_count"] == 1
    assert any(a["summary"] == "Context alert" for a in body["alerts"]["items"])


# ---------------------------------------------------------------------------
# Markdown negotiation
# ---------------------------------------------------------------------------


async def test_context_markdown_variant(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)
    r = await http_client.get(_CONTEXT_URL, headers={"Accept": "text/markdown"})
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert r.text.startswith("# Portfolio context")
    with pytest.raises(json.JSONDecodeError):
        r.json()


async def test_asset_markdown_variant(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    asset_id = await _seed_asset(db_session_factory, "0x" + "7" * 40, symbol="MDN")
    r = await http_client.get(f"/api/v1/llm/asset/{asset_id}", headers={"Accept": "text/markdown"})
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    assert r.text.startswith("# MDN")


async def test_context_default_is_json(http_client: httpx.AsyncClient) -> None:
    await _setup_and_login(http_client)
    r = await http_client.get(_CONTEXT_URL)
    assert "application/json" in r.headers["content-type"]
    r.json()  # does not raise


# ---------------------------------------------------------------------------
# Size budget
# ---------------------------------------------------------------------------


async def test_context_response_stays_within_size_budget(
    http_client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup_and_login(http_client)
    wallet_id = await _seed_wallet(db_session_factory)

    holdings = []
    for i in range(30):
        asset_id = await _seed_asset(
            db_session_factory, "0x" + str(i).zfill(2) + "9" * 38, symbol=f"SYM{i}"
        )
        holdings.append(
            {
                "wallet_id": wallet_id,
                "asset_id": asset_id,
                "raw_amount": "1000000000000000000",
                "price_usd": "1.000000000000000000",
                "value_usd": f"{i + 1}.000000000000000000",
            }
        )
        news_id = await _seed_news(db_session_factory, asset_id, title=f"Headline number {i}")
        await _seed_alert(db_session_factory, news_id, asset_id, summary=f"Alert number {i}")

    await _seed_snapshot(db_session_factory, holdings=holdings)

    r = await http_client.get(_CONTEXT_URL)
    assert r.status_code == 200
    body = r.json()
    assert len(body["top_allocations"]) <= 8
    assert len(body["headlines"]) <= 5
    assert len(body["alerts"]["items"]) <= 5
    assert len(r.content) < _CONTEXT_CHAR_BUDGET
    assert any("top 8 of 30" in c for c in body["caveats"])
