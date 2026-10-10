"""Streamed current-portfolio and full-history JSON/CSV exports (T084 / US4)."""

from __future__ import annotations

import csv
import io
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.csv_safe import sanitize
from audr.operations.init_key import get_master_key
from audr.wallets.service import decrypt_address

_log = logging.getLogger(__name__)

# Cap streaming exports to avoid runaway memory / response time.  When hit,
# a warning is logged and the export is truncated at this row count.
_MAX_HISTORY_EXPORT_ROWS = 5_000_000


async def export_current_portfolio(session: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Export the current portfolio state as a structured dict.

    Uses the latest balance_observation per (wallet, asset) pair.
    Excluded assets are exported too (AUD-447) — the export is a record of
    what the wallets hold, and exclusion only decides what counts towards the
    portfolio's value. The full-history export has always behaved this way;
    omitting them here made a round-trip through the export lose holdings.
    Unknown balance (no observation) is represented as ``None``, never ``0``.

    Returns::

        {
            "schema_version": 1,
            "record_type": "current_portfolio",
            "exported_at": "<ISO-8601>",
            "holdings": [
                {
                    "wallet_address": str,
                    "asset_symbol": str,
                    "asset_name": str,
                    "token_address": str,
                    "raw_amount": str | None,
                    "decimals": int,
                },
                ...
            ]
        }
    """
    result = await session.execute(
        sa.text(
            """
            SELECT
                w.id              AS wallet_id,
                w.address_ciphertext,
                a.symbol          AS asset_symbol,
                a.name            AS asset_name,
                a.token_address,
                bo.raw_amount,
                COALESCE(a.decimals_override, a.decimals) AS decimals
            FROM wallet w
            CROSS JOIN asset a
            LEFT JOIN LATERAL (
                SELECT raw_amount
                FROM balance_observation
                WHERE wallet_id = w.id
                  AND asset_id  = a.id
                ORDER BY observed_at DESC
                LIMIT 1
            ) bo ON true
            ORDER BY w.id, a.symbol
            """
        )
    )
    rows = result.fetchall()

    # This export is bounded by wallet count × asset count (never streamed),
    # so decrypting every row and re-sorting by the plaintext address in
    # Python — matching the pre-AUD-490 alphabetical-by-address order — costs
    # nothing worth avoiding. Addresses are decrypted once per wallet via
    # this cache rather than once per (wallet, asset) row.
    address_cache: dict[object, str] = {}
    key = await get_master_key(session) if rows else None
    holdings = []
    for row in rows:
        wallet_id, address_ciphertext = uuid.UUID(str(row[0])), row[1]
        if wallet_id not in address_cache:
            address_cache[wallet_id] = decrypt_address(address_ciphertext, wallet_id, key)
        raw_amount = row[5]  # Decimal or None
        holdings.append(
            {
                "wallet_address": address_cache[wallet_id],
                "asset_symbol": str(row[2]),
                "asset_name": str(row[3]),
                "token_address": str(row[4]),
                "raw_amount": str(Decimal(raw_amount)) if raw_amount is not None else None,
                "decimals": int(row[6]),
            }
        )
    holdings.sort(key=lambda h: (h["wallet_address"], h["asset_symbol"]))

    return {
        "schema_version": 1,
        "record_type": "current_portfolio",
        "exported_at": datetime.now(UTC).isoformat(),
        "holdings": holdings,
    }


# Single flat query shared by export_full_history and stream_history_csv.
# LEFT JOIN valuation_line so that snapshots with no lines still appear
# (they produce a row with NULLs for the line columns).
#
# Ordered by w.id rather than the decrypted address (AUD-490): a GCM envelope's
# nonce is random per row, so ordering by address_ciphertext would be
# meaningless, and both queries feed a row-by-row streaming CSV export that
# cannot buffer the full result set just to sort on plaintext. w.id is stable
# and deterministic, which is all the "deterministic row order" requirement
# needs — it does not have to be alphabetical-by-address.
_HISTORY_QUERY = sa.text(
    """
    SELECT
        vs.id                                             AS snapshot_id,
        vs.snapshotted_at,
        vs.quality,
        w.id                                               AS wallet_id,
        w.address_ciphertext,
        a.symbol                                          AS asset_symbol,
        COALESCE(amr.name, a.name)                        AS asset_name,
        vl.raw_amount,
        vl.price_usd,
        COALESCE(a.decimals_override, a.decimals)         AS decimals
    FROM valuation_snapshot vs
    LEFT JOIN valuation_line vl      ON vl.snapshot_id = vs.id
    LEFT JOIN wallet w               ON w.id = vl.wallet_id
    LEFT JOIN asset a                ON a.id = vl.asset_id
    LEFT JOIN LATERAL (
        SELECT name
        FROM asset_metadata_revision
        WHERE asset_id = a.id
          AND recorded_at <= vs.snapshotted_at
        ORDER BY recorded_at DESC
        LIMIT 1
    ) amr ON true
    WHERE (:from_ IS NULL OR vs.snapshotted_at >= :from_)
      AND (:to_   IS NULL OR vs.snapshotted_at <= :to_)
    ORDER BY vs.snapshotted_at ASC, w.id, a.symbol
    """
).bindparams(
    sa.bindparam("from_", type_=sa.TIMESTAMP(timezone=True)),
    sa.bindparam("to_", type_=sa.TIMESTAMP(timezone=True)),
)

# For streaming CSV we use INNER JOINs: snapshots with no lines produce
# no CSV rows, which is the correct semantic.
_HISTORY_STREAM_QUERY = sa.text(
    """
    SELECT
        vs.id                                             AS snapshot_id,
        vs.snapshotted_at,
        vs.quality,
        w.id                                               AS wallet_id,
        w.address_ciphertext,
        a.symbol                                          AS asset_symbol,
        COALESCE(amr.name, a.name)                        AS asset_name,
        vl.raw_amount,
        vl.price_usd,
        COALESCE(a.decimals_override, a.decimals)         AS decimals
    FROM valuation_snapshot vs
    JOIN valuation_line vl           ON vl.snapshot_id = vs.id
    JOIN wallet w                    ON w.id = vl.wallet_id
    JOIN asset a                     ON a.id = vl.asset_id
    LEFT JOIN LATERAL (
        SELECT name
        FROM asset_metadata_revision
        WHERE asset_id = a.id
          AND recorded_at <= vs.snapshotted_at
        ORDER BY recorded_at DESC
        LIMIT 1
    ) amr ON true
    WHERE (:from_ IS NULL OR vs.snapshotted_at >= :from_)
      AND (:to_   IS NULL OR vs.snapshotted_at <= :to_)
    ORDER BY vs.snapshotted_at ASC, w.id, a.symbol
    """
).bindparams(
    sa.bindparam("from_", type_=sa.TIMESTAMP(timezone=True)),
    sa.bindparam("to_", type_=sa.TIMESTAMP(timezone=True)),
)


async def export_full_history(
    session: AsyncSession,
    from_: datetime | None = None,
    to_: datetime | None = None,
) -> dict:  # type: ignore[type-arg]
    """Export the complete valuation snapshot history as a structured dict.

    Uses a single flat JOIN query instead of N+1 per-snapshot queries.
    Snapshots are ordered by ``snapshotted_at`` ASC.  Each line's asset name
    comes from the ``asset_metadata_revision`` recorded latest before the
    snapshot's ``snapshotted_at``; falls back to the current ``asset.name`` when
    no revision predates the snapshot.

    Returns::

        {
            "schema_version": 1,
            "record_type": "full_history",
            "exported_at": "<ISO-8601>",
            "snapshots": [
                {
                    "snapshot_id": str,
                    "snapshotted_at": str,
                    "quality": str,
                    "lines": [
                        {
                            "wallet_address": str,
                            "asset_symbol": str,
                            "asset_name": str,
                            "raw_amount": str | None,
                            "price_usd": str | None,
                            "decimals": int,
                        },
                        ...
                    ]
                },
                ...
            ]
        }
    """
    result = await session.execute(
        _HISTORY_QUERY,
        {"from_": from_, "to_": to_},
    )
    rows = result.fetchall()

    snap_map: dict[str, dict] = {}  # type: ignore[type-arg]
    snap_order: list[dict] = []  # type: ignore[type-arg]

    address_cache: dict[object, str] = {}
    key = await get_master_key(session) if rows else None
    for row in rows:
        snap_id = str(row[0])
        snapshotted_at: datetime = row[1]
        quality: str = row[2]
        wallet_id = uuid.UUID(str(row[3])) if row[3] is not None else None
        address_ciphertext = row[4]

        if snap_id not in snap_map:
            entry: dict = {  # type: ignore[type-arg]
                "snapshot_id": snap_id,
                "snapshotted_at": snapshotted_at.isoformat(),
                "quality": quality,
                "lines": [],
            }
            snap_map[snap_id] = entry
            snap_order.append(entry)

        if wallet_id is not None:
            if wallet_id not in address_cache:
                address_cache[wallet_id] = decrypt_address(address_ciphertext, wallet_id, key)
            raw_amount = row[7]
            price_usd = row[8]
            snap_map[snap_id]["lines"].append(
                {
                    "wallet_address": address_cache[wallet_id],
                    "asset_symbol": str(row[5]),
                    "asset_name": str(row[6]),
                    "raw_amount": str(Decimal(raw_amount)) if raw_amount is not None else None,
                    "price_usd": str(Decimal(price_usd)) if price_usd is not None else None,
                    "decimals": int(row[9]),
                }
            )

    return {
        "schema_version": 1,
        "record_type": "full_history",
        "exported_at": datetime.now(UTC).isoformat(),
        "snapshots": snap_order,
    }


def _csv_row(*cells: object) -> str:
    """Render one CSV row as a string (including newline)."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(cells)
    return buf.getvalue()


async def stream_history_csv(
    session: AsyncSession,
    from_: datetime | None = None,
    to_: datetime | None = None,
) -> AsyncGenerator[str]:
    """Stream the full valuation history as CSV chunks, row by row.

    Uses a server-side cursor (session.stream) so the entire result set is
    never held in memory.  Honors ``from_`` / ``to_`` date filters.
    Logs a warning and stops early if the export exceeds
    ``_MAX_HISTORY_EXPORT_ROWS``.

    Format::

        # schema_version: 1
        snapshot_id,snapshotted_at,quality,wallet_address,asset_symbol,asset_name,raw_amount,price_usd,decimals
    """
    yield "# schema_version: 1\n"
    yield _csv_row(
        "snapshot_id",
        "snapshotted_at",
        "quality",
        "wallet_address",
        "asset_symbol",
        "asset_name",
        "raw_amount",
        "price_usd",
        "decimals",
    )

    stream = await session.stream(_HISTORY_STREAM_QUERY, {"from_": from_, "to_": to_})
    stream = stream.yield_per(500)

    # Master key fetched lazily on the first row (never, for an empty
    # export), then reused — decrypted addresses are cached per wallet_id so
    # a wallet with many lines across many snapshots is decrypted once.
    key: bytes | None = None
    address_cache: dict[uuid.UUID, str] = {}

    row_count = 0
    async for row in stream:
        if key is None:
            key = await get_master_key(session)
        wallet_id = uuid.UUID(str(row[3]))
        if wallet_id not in address_cache:
            address_cache[wallet_id] = decrypt_address(row[4], wallet_id, key)
        raw_amount = str(Decimal(row[7])) if row[7] is not None else ""
        price_usd = str(Decimal(row[8])) if row[8] is not None else ""
        yield _csv_row(
            str(row[0]),
            row[1].isoformat(),
            str(row[2]),
            sanitize(address_cache[wallet_id]),
            sanitize(str(row[5])),
            sanitize(str(row[6])),
            raw_amount,
            price_usd,
            int(row[9]),
        )
        row_count += 1
        if row_count >= _MAX_HISTORY_EXPORT_ROWS:
            _log.warning(
                "history export truncated at %d rows (from_=%s, to_=%s)",
                _MAX_HISTORY_EXPORT_ROWS,
                from_,
                to_,
            )
            return


async def render_portfolio_csv(session: AsyncSession) -> str:
    """Render the current portfolio as a CSV string.

    Format::

        # schema_version: 1
        wallet_address,asset_symbol,asset_name,token_address,raw_amount,decimals
        0xABC...,ETH,Ether,0x000...,1000000000000000000,18
        0xABC...,USDC,USD Coin,0x123...,,6

    Unknown balances are serialised as empty strings, not ``"0"``.
    Uses Python's ``csv`` module; no float arithmetic.
    """
    portfolio = await export_current_portfolio(session)

    buf = io.StringIO()
    buf.write("# schema_version: 1\n")

    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["wallet_address", "asset_symbol", "asset_name", "token_address", "raw_amount", "decimals"]
    )

    for holding in portfolio["holdings"]:
        raw_amount_cell = holding["raw_amount"] if holding["raw_amount"] is not None else ""
        writer.writerow(
            [
                sanitize(holding["wallet_address"]),
                sanitize(holding["asset_symbol"]),
                sanitize(holding["asset_name"]),
                sanitize(holding["token_address"]),
                raw_amount_cell,
                holding["decimals"],
            ]
        )

    return buf.getvalue()


async def render_history_csv(
    session: AsyncSession,
    from_: datetime | None = None,
    to_: datetime | None = None,
) -> str:
    """Render the full valuation history as a CSV string (buffers for compatibility).

    Prefer ``stream_history_csv`` for large exports.
    """
    return "".join([chunk async for chunk in stream_history_csv(session, from_=from_, to_=to_)])
