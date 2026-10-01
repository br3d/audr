"""Integration tests for CoinMarketCap address/symbol resolution (AUD-358).

Exercises audr.assets.cmc_catalog against the real vendored map snapshot
(backend/src/audr/assets/data/cmc_map_seed.json, fetched from CoinMarketCap's
live keyless /cryptocurrency/map on 2026-10-01) — no network calls, but real
data, so these tests catch resolution regressions the way the issue's own
investigation did.

Marker: integration
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.cmc_catalog import import_cmc_map, resolve_cmc_ids
from tests.fixtures.seed import DAI_ADDRESS, USDC_ADDRESS, WETH_ADDRESS

pytestmark = pytest.mark.integration

# Legacy Polygon MATIC contract — pinned in cmc_catalog._PINS because CMC's
# map no longer carries a "MATIC" symbol or this address at all (AUD-358).
_MATIC_ADDRESS = "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0"
_MATIC_CMC_ID = 28321

# An address with no asset row and no map entry — must stay unresolved.
_UNKNOWN_ADDRESS = "0x" + "9" * 40


async def _insert_asset(session: AsyncSession, *, address: str, symbol: str) -> None:
    await session.execute(
        text(
            "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
            " VALUES (:id, :addr, :sym, :sym, 18, 'manual')"
            " ON CONFLICT (token_address) DO NOTHING"
        ),
        {"id": str(uuid.uuid4()), "addr": address.lower(), "sym": symbol},
    )


@pytest.mark.integration
async def test_usdc_on_ethereum_resolves_via_symbol_fallback(
    db_session: AsyncSession,
) -> None:
    """Regression (AUD-358): USDC's CMC-canonical platform is zkSync, not
    Ethereum, so it has no address match in the map — only the unique-symbol
    fallback finds it. This is the exact trap the issue investigation found;
    losing it silently reprices one of the most commonly held ERC-20s as
    unknown."""
    await import_cmc_map(db_session)
    await _insert_asset(db_session, address=USDC_ADDRESS, symbol="USDC")
    await db_session.flush()

    resolved = await resolve_cmc_ids(db_session, [USDC_ADDRESS])

    assert resolved.get(USDC_ADDRESS.lower()) == 3408


@pytest.mark.integration
async def test_dai_and_weth_resolve_via_address_match(
    db_session: AsyncSession,
) -> None:
    """Common majors resolve straight from the map's Ethereum platform address,
    without needing a pin or even an asset row."""
    await import_cmc_map(db_session)

    resolved = await resolve_cmc_ids(db_session, [DAI_ADDRESS, WETH_ADDRESS])

    assert DAI_ADDRESS.lower() in resolved
    assert WETH_ADDRESS.lower() in resolved


@pytest.mark.integration
async def test_pinned_address_resolves_without_map_lookup(
    db_session: AsyncSession,
) -> None:
    """A manual pin resolves even with no map imported at all — pins never
    depend on the synced/vendored table."""
    resolved = await resolve_cmc_ids(db_session, [_MATIC_ADDRESS])

    assert resolved.get(_MATIC_ADDRESS) == _MATIC_CMC_ID


@pytest.mark.integration
async def test_unknown_address_left_unresolved_not_guessed(
    db_session: AsyncSession,
) -> None:
    """An address with no pin, no map address match, and no asset row (so no
    symbol to fall back on) is simply absent — never guessed at."""
    await import_cmc_map(db_session)

    resolved = await resolve_cmc_ids(db_session, [_UNKNOWN_ADDRESS])

    assert _UNKNOWN_ADDRESS not in resolved
    assert resolved == {}


@pytest.mark.integration
async def test_ambiguous_symbol_without_pin_left_unresolved(
    db_session: AsyncSession,
) -> None:
    """A ticker that matches more than one CMC id and has no pin must not be
    guessed — this is the "unresolved, not guessed" rule from the issue
    (e.g. CRO/SOL/MIM in the live map each map to multiple ids)."""
    await import_cmc_map(db_session)
    sol_address = "0x" + "a1" * 20
    await _insert_asset(db_session, address=sol_address, symbol="SOL")
    await db_session.flush()

    resolved = await resolve_cmc_ids(db_session, [sol_address])

    assert sol_address not in resolved


@pytest.mark.integration
async def test_empty_address_list_returns_empty(db_session: AsyncSession) -> None:
    """No addresses requested -> no DB work, empty result."""
    resolved = await resolve_cmc_ids(db_session, [])

    assert resolved == {}
