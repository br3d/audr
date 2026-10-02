"""Integration tests for the asset_icon_refresh job handler (AUD-385).

Marker: integration
"""

from __future__ import annotations

import uuid

import pytest
import respx
from httpx import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.asset_icons import handle_asset_icon_refresh
from audr.jobs.policy import get_shared_asset_icon_rate_limiter
from audr.providers.asset_icons import trust_wallet_logo_url

pytestmark = pytest.mark.integration

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 32


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wallet (id, address, label, status)"
            " VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    return wallet_id


async def _insert_asset(session: AsyncSession, *, token_address: str, symbol: str) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source)
            VALUES (:id, :addr, :sym, :sym, 18, 'manual')
            """
        ),
        {"id": str(asset_id), "addr": token_address.lower(), "sym": symbol},
    )
    return asset_id


async def _insert_balance(
    session: AsyncSession, *, wallet_id: uuid.UUID, asset_id: uuid.UUID
) -> None:
    await session.execute(
        text(
            "INSERT INTO balance_observation"
            " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
            " VALUES (:id, :wallet, :asset, 1000000000000000000, 100, now())"
        ),
        {"id": str(uuid.uuid4()), "wallet": str(wallet_id), "asset": str(asset_id)},
    )


async def _insert_icon_row(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    status: str,
    fetched_at_sql: str,
) -> None:
    await session.execute(
        text(
            f"""
            INSERT INTO asset_icon (asset_id, status, fetched_at)
            VALUES (:asset_id, :status, {fetched_at_sql})
            """  # noqa: S608 — fetched_at_sql is a module-local constant, not user input
        ),
        {"asset_id": str(asset_id), "status": status},
    )


async def _get_icon_row(session: AsyncSession, asset_id: uuid.UUID) -> dict | None:
    result = await session.execute(
        text(
            "SELECT content_type, image, source, status FROM asset_icon WHERE asset_id = :id"
        ),
        {"id": str(asset_id)},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    get_shared_asset_icon_rate_limiter.cache_clear()
    yield
    get_shared_asset_icon_rate_limiter.cache_clear()


@pytest.mark.integration
async def test_caches_trust_wallet_icon_when_available(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    run_id = uuid.uuid4()
    addr = "0x" + "aa" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "1" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="TWI")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    tw_url = trust_wallet_logo_url(addr)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(tw_url).mock(
            return_value=Response(200, content=_PNG_BYTES, headers={"content-type": "image/png"})
        )
        await handle_asset_icon_refresh(db_session, run_id)

    row = await _get_icon_row(db_session, asset_id)
    assert row is not None
    assert row["status"] == "ok"
    assert row["source"] == "trustwallet"
    assert bytes(row["image"]) == _PNG_BYTES
    assert row["content_type"] == "image/png"


@pytest.mark.integration
async def test_falls_back_to_coingecko_when_trust_wallet_misses(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    run_id = uuid.uuid4()
    addr = "0x" + "bb" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "2" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="CGI")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    tw_url = trust_wallet_logo_url(addr)
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{addr.lower()}"
    image_url = "https://assets.coingecko.com/coins/images/1/small/cgi.png"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(tw_url).mock(return_value=Response(404))
        mock.get(contract_url).mock(
            return_value=Response(200, json={"image": {"small": image_url}})
        )
        mock.get(image_url).mock(
            return_value=Response(200, content=_PNG_BYTES, headers={"content-type": "image/png"})
        )
        await handle_asset_icon_refresh(db_session, run_id)

    row = await _get_icon_row(db_session, asset_id)
    assert row is not None
    assert row["status"] == "ok"
    assert row["source"] == "coingecko"


@pytest.mark.integration
async def test_negative_caches_when_both_sources_miss(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    run_id = uuid.uuid4()
    addr = "0x" + "cc" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "3" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="NOP")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    tw_url = trust_wallet_logo_url(addr)
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{addr.lower()}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(tw_url).mock(return_value=Response(404))
        mock.get(contract_url).mock(return_value=Response(404))
        await handle_asset_icon_refresh(db_session, run_id)

    row = await _get_icon_row(db_session, asset_id)
    assert row is not None
    assert row["status"] == "missing"
    assert row["image"] is None


@pytest.mark.integration
async def test_recent_negative_cache_suppresses_refetch(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    run_id = uuid.uuid4()
    addr = "0x" + "dd" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "4" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="SKIP")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_icon_row(
        db_session, asset_id=asset_id, status="missing", fetched_at_sql="now() - interval '1 day'"
    )
    await db_session.flush()

    with respx.mock(assert_all_called=True) as mock:
        # No routes registered at all — any HTTP call here fails the test.
        await handle_asset_icon_refresh(db_session, run_id)
        assert len(mock.calls) == 0


@pytest.mark.integration
async def test_stale_negative_cache_is_retried(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    run_id = uuid.uuid4()
    addr = "0x" + "ee" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "5" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="OLD")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_icon_row(
        db_session, asset_id=asset_id, status="missing", fetched_at_sql="now() - interval '8 days'"
    )
    await db_session.flush()

    tw_url = trust_wallet_logo_url(addr)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(tw_url).mock(
            return_value=Response(200, content=_PNG_BYTES, headers={"content-type": "image/png"})
        )
        await handle_asset_icon_refresh(db_session, run_id)

    row = await _get_icon_row(db_session, asset_id)
    assert row is not None
    assert row["status"] == "ok"


@pytest.mark.integration
async def test_rate_limited_coingecko_leaves_no_row_for_retry(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    run_id = uuid.uuid4()
    addr = "0x" + "ff" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "6" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="RL")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    tw_url = trust_wallet_logo_url(addr)
    contract_url = f"{_COINGECKO_BASE}/coins/ethereum/contract/{addr.lower()}"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(tw_url).mock(return_value=Response(404))
        mock.get(contract_url).mock(return_value=Response(429))
        await handle_asset_icon_refresh(db_session, run_id)

    row = await _get_icon_row(db_session, asset_id)
    # Rate-limited, not missing: no row written at all, so the next run retries.
    assert row is None


@pytest.mark.integration
async def test_disabled_flag_makes_no_outbound_request(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from audr.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/audr")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key")
    monkeypatch.setenv("ASSET_ICONS_REMOTE_FETCH", "false")
    get_settings.cache_clear()

    run_id = uuid.uuid4()
    addr = "0x" + "01" * 20
    wallet_id = await _insert_wallet(db_session, "0xface" + "7" * 36)
    asset_id = await _insert_asset(db_session, token_address=addr, symbol="OFF")
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await db_session.flush()

    try:
        with respx.mock(assert_all_called=True) as mock:
            await handle_asset_icon_refresh(db_session, run_id)
            assert len(mock.calls) == 0
    finally:
        get_settings.cache_clear()

    assert await _get_icon_row(db_session, asset_id) is None
