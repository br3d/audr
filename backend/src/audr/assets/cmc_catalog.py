"""CoinMarketCap address/symbol -> cmc_id resolution (AUD-358).

The CoinMarketCap keyless price endpoint only accepts numeric CMC ids, not
ERC-20 contract addresses, so pricing with it requires a resolution table.
That table is built the same way `audr.assets.catalog` builds the Uniswap
token catalog: a vendored snapshot gives a fresh install usable data with no
network call at startup (AUD-357's lesson applied here), and
`sync_cmc_map_live` can refresh it from CoinMarketCap's keyless
`/cryptocurrency/map` endpoint once the worker is running.

Resolution order, matched to the investigation in AUD-358:
  1. Manual pin (`_PINS`) — for coins whose current CMC "canonical" platform
     address doesn't match any contract our catalog actually uses (a rebrand,
     a contract migration), verified by hand against a live map pull.
  2. Address match — the CMC map's `platform.id == 1` (Ethereum mainnet)
     contract address, matched directly.
  3. Unique symbol match — only when exactly one CMC id anywhere in the map
     shares that ticker. An ambiguous ticker (multiple ids, no pin) is left
     unresolved rather than guessed — this is a pricing feed, not a search box.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.models import CmcMapEntry, CmcMapVersion
from audr.providers.coinmarketcap_public import CoinMarketCapProvider

_VENDORED_MAP_PATH = importlib.resources.files("audr.assets") / "data" / "cmc_map_seed.json"

_MAP_PAGE_SIZE = 5000
_MAP_TTL = timedelta(days=1)

# Manual pins (AUD-358): CMC's keyless map only exposes a coin's current
# "canonical" platform contract, not every address it has ever lived at. A
# rebrand, a contract migration, or two coins sharing a ticker can leave the
# address/symbol auto-match wrong or silent for well-known real holdings.
# Verified by hand against a live `/cryptocurrency/map` pull (2026-10-01) —
# do not add an entry here without checking the live id yourself; a wrong
# pin silently mispríces a real holding, which is worse than leaving it
# unresolved.
_PINS: dict[str, int] = {
    # Polygon's token migrated MATIC -> POL (1:1 swap). CMC's map now only
    # lists the new POL contract as the Ethereum platform address, so the
    # legacy MATIC ERC-20 contract has no address match in the map at all,
    # and the "MATIC" ticker no longer exists in CMC's data either.
    "0x7d1afa7b718fb893db30a3abc0cfc608aacfebb0": 28321,  # MATIC -> POL
    # Paxos rebranded PAX -> USDP and redeployed on a new contract; the old
    # Paxos Standard contract (still in circulation and in our catalog)
    # isn't CMC's canonical address for "Pax Dollar" (3330), and "USDP" is
    # otherwise ambiguous with an unrelated duplicate-ticker coin (8886).
    "0xc1d204d77861def49b6e769347a883b15ec397ff": 3330,  # PAX -> USDP (Pax Dollar)
    # Augur v2's REPv2 contract isn't separately listed by CMC — "REP"
    # (id 1104) is the only Augur entry in the map and tracks the v2 token.
    "0x221657776846890989a759ba2973e427dff5c9bb": 1104,  # REPv2 -> REP (Augur)
}


class CmcMapImportError(Exception):
    """Raised when a CMC map snapshot (vendored or synced) cannot be read or parsed."""


async def import_cmc_map(
    session: AsyncSession,
    *,
    path: Path | Any = _VENDORED_MAP_PATH,
) -> CmcMapVersion:
    """Import the vendored CMC map snapshot into cmc_map_version + cmc_map_entry.

    Idempotent: if this exact snapshot (identified by a content hash) is
    already imported, returns the existing version without re-reading rows.
    """
    entries, source_hash = _load_vendored_entries(path)
    return await _import_entries(session, entries, source_hash)


async def sync_cmc_map_live(
    session: AsyncSession, provider: CoinMarketCapProvider
) -> CmcMapVersion:
    """Fetch the full keyless map from CoinMarketCap and import it as a new version.

    Paginates until a page returns fewer rows than requested. Propagates
    RateLimitError/CoinMarketCapError on failure — callers decide whether a
    failed refresh should fail their job or just leave the existing
    (possibly stale) version in place.
    """
    raw_rows: list[dict[str, Any]] = []
    start = 1
    while True:
        page = await provider.fetch_map_page(start=start, limit=_MAP_PAGE_SIZE)
        raw_rows.extend(page)
        if len(page) < _MAP_PAGE_SIZE:
            break
        start += _MAP_PAGE_SIZE

    entries = _normalise_raw_rows(raw_rows)
    content = json.dumps(entries, sort_keys=True).encode()
    source_hash = f"live:{hashlib.sha256(content).hexdigest()[:16]}"
    return await _import_entries(session, entries, source_hash)


async def get_latest_cmc_map_version(session: AsyncSession) -> CmcMapVersion | None:
    """Return the most recently imported CMC map version, or None."""
    from sqlalchemy import select

    result = await session.execute(
        select(CmcMapVersion).order_by(CmcMapVersion.imported_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def is_cmc_map_stale(session: AsyncSession) -> bool:
    """True when no map has ever been imported, or the latest is older than the TTL."""
    version = await get_latest_cmc_map_version(session)
    if version is None:
        return True
    return datetime.now(UTC) - version.imported_at > _MAP_TTL


async def resolve_cmc_ids(session: AsyncSession, token_addresses: list[str]) -> dict[str, int]:
    """Resolve lowercase ERC-20 addresses to CoinMarketCap ids.

    Matches the AddressResolver contract CoinMarketCapProvider.get_prices
    expects: addresses that can't be resolved (no pin, no address match, an
    ambiguous symbol) are simply absent from the result, never guessed.
    """
    if not token_addresses:
        return {}

    addresses = [a.lower() for a in token_addresses]
    result: dict[str, int] = {}
    remaining: list[str] = []
    for addr in addresses:
        pin = _PINS.get(addr)
        if pin is not None:
            result[addr] = pin
        else:
            remaining.append(addr)

    if not remaining:
        return result

    version = await get_latest_cmc_map_version(session)
    if version is None:
        return result

    addr_rows = await session.execute(
        sa.select(CmcMapEntry.eth_address, CmcMapEntry.cmc_id).where(
            CmcMapEntry.version_id == version.id,
            CmcMapEntry.eth_address.in_(remaining),
        )
    )
    for addr, cmc_id in addr_rows:
        result[addr] = cmc_id
    remaining = [a for a in remaining if a not in result]

    if not remaining:
        return result

    # Symbol fallback: addresses the map doesn't carry at all (e.g. USDC —
    # CMC's canonical platform for it is zkSync, not Ethereum) are matched
    # via our own catalog's symbol, but only when that ticker is unique
    # across the *entire* CMC id space.
    asset_rows = await session.execute(
        sa.text("SELECT token_address, symbol FROM asset WHERE token_address = ANY(:addrs)"),
        {"addrs": remaining},
    )
    symbol_by_addr = {row[0]: row[1].upper() for row in asset_rows}
    wanted_symbols = set(symbol_by_addr.values())
    if not wanted_symbols:
        return result

    symbol_rows = await session.execute(
        sa.select(CmcMapEntry.symbol, CmcMapEntry.cmc_id).where(
            CmcMapEntry.version_id == version.id,
            CmcMapEntry.symbol.in_(wanted_symbols),
        )
    )
    ids_by_symbol: dict[str, set[int]] = {}
    for symbol, cmc_id in symbol_rows:
        ids_by_symbol.setdefault(symbol.upper(), set()).add(cmc_id)

    for addr in remaining:
        symbol = symbol_by_addr.get(addr)
        if symbol is None:
            continue
        candidates = ids_by_symbol.get(symbol, set())
        if len(candidates) == 1:
            result[addr] = next(iter(candidates))
        # 0 or >1 candidates: leave unresolved, never guess.

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_raw_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trim raw `/cryptocurrency/map` rows to {id, symbol, eth_address}."""
    entries: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {"id": row["id"], "symbol": row["symbol"]}
        platform = row.get("platform")
        if platform and platform.get("id") == 1 and platform.get("token_address"):
            entry["eth_address"] = platform["token_address"].lower()
        entries.append(entry)
    return entries


def _load_vendored_entries(path: Path | Any) -> tuple[list[dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CmcMapImportError(f"Failed to read CMC map file {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except Exception as exc:
        raise CmcMapImportError("CMC map file is not valid JSON") from exc

    if not isinstance(data, dict) or "entries" not in data:
        raise CmcMapImportError("Unrecognised CMC map format")

    source_hash = f"vendored:{hashlib.sha256(raw).hexdigest()[:16]}"
    return data["entries"], source_hash


async def _import_entries(
    session: AsyncSession, entries: list[dict[str, Any]], source_hash: str
) -> CmcMapVersion:
    from sqlalchemy import select

    existing = await session.execute(
        select(CmcMapVersion).where(CmcMapVersion.source_hash == source_hash)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row

    version = CmcMapVersion(
        id=uuid.uuid4(),
        source_hash=source_hash,
        entry_count=len(entries),
    )
    session.add(version)
    await session.flush()

    for entry in entries:
        session.add(
            CmcMapEntry(
                id=uuid.uuid4(),
                version_id=version.id,
                cmc_id=entry["id"],
                symbol=entry["symbol"],
                eth_address=entry.get("eth_address"),
            )
        )

    await session.flush()
    return version
