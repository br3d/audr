"""AUD-447: exclude removes an asset from the portfolio's value, nothing else.

The owner's decision on the card: excluding an asset must not stop tracking it
("только убрать из стоимости, сканировать дальше"), and must not hide it
anywhere outside the dashboard and the Assets list. Four worker queries used
`excluded` as a tracking switch instead, which had two visible consequences:

  - re-including an asset showed a stale or missing price until the next
    hourly refresh, even though nothing about the price had been invalidated;
  - the event indexer advanced its per-wallet checkpoint past the blocks it
    skipped while the asset was excluded, so the hole it punched in the
    Events/Allowances views survived the re-include permanently.

Marker: integration
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.asset_icons import _get_assets_needing_icon
from audr.jobs.event_indexer import _get_tracked_token_addresses
from audr.jobs.news import _get_held_assets
from audr.jobs.quotes import _get_held_asset_addresses

pytestmark = pytest.mark.integration


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO wallet (id, address, label_ciphertext, status) VALUES (:id, :addr, '', 'active')"),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    return wallet_id


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str,
    excluded: bool,
) -> uuid.UUID:
    asset_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
            VALUES (:id, :addr, :sym, :sym, 18, 'manual', :excluded)
            """
        ),
        {"id": str(asset_id), "addr": token_address.lower(), "sym": symbol, "excluded": excluded},
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


@pytest.fixture
async def _held_excluded_asset(db_session: AsyncSession) -> tuple[uuid.UUID, str]:
    """One wallet holding one excluded asset and one included one."""
    wallet_id = await _insert_wallet(db_session, "0x" + "a" * 40)
    # Not "0xeee…" — that sentinel is native ETH, which already exists.
    excluded_address = "0x" + "b" * 40
    excluded_id = await _insert_asset(
        db_session, token_address=excluded_address, symbol="EXCL", excluded=True
    )
    included_id = await _insert_asset(
        db_session, token_address="0x" + "c" * 40, symbol="INCL", excluded=False
    )
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=excluded_id)
    await _insert_balance(db_session, wallet_id=wallet_id, asset_id=included_id)
    return excluded_id, excluded_address


async def test_quote_refresh_still_prices_an_excluded_asset(
    db_session: AsyncSession,
    _held_excluded_asset: tuple[uuid.UUID, str],
) -> None:
    """A held-but-excluded asset is still a pricing candidate.

    Without this, re-including the asset leaves the dashboard showing an
    unpriced row until the next quote_refresh happens to run.
    """
    _, excluded_address = _held_excluded_asset

    addresses = await _get_held_asset_addresses(db_session)

    assert excluded_address in addresses
    assert "0x" + "c" * 40 in addresses


async def test_event_indexer_still_tracks_an_excluded_asset(
    db_session: AsyncSession,
    _held_excluded_asset: tuple[uuid.UUID, str],
) -> None:
    """The indexer keeps reading an excluded asset's transfers.

    This is the one surface where skipping is not recoverable: the checkpoint
    advances regardless, so blocks skipped while excluded are never re-read.
    """
    _, excluded_address = _held_excluded_asset

    addresses = await _get_tracked_token_addresses(db_session)

    assert excluded_address in addresses


async def test_news_refresh_still_covers_an_excluded_asset(
    db_session: AsyncSession,
    _held_excluded_asset: tuple[uuid.UUID, str],
) -> None:
    """News keeps being fetched for an asset the owner still holds."""
    excluded_id, _ = _held_excluded_asset

    assets = await _get_held_assets(db_session)

    assert excluded_id in {a.id for a in assets}


async def test_icon_refresh_still_covers_an_excluded_asset(
    db_session: AsyncSession,
    _held_excluded_asset: tuple[uuid.UUID, str],
) -> None:
    """Excluded rows are still rendered behind "Show excluded", so they need an icon."""
    excluded_id, _ = _held_excluded_asset

    candidates = await _get_assets_needing_icon(db_session)

    assert excluded_id in {asset_id for asset_id, _ in candidates}
