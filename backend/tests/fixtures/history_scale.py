"""Scale fixture generator for history performance testing (T074 / US3 / AUD-87).

Generates:
  - 50 wallets
  - 100 assets
  - 500 held (wallet, asset) pairs  (random subset of 50×100 = 5,000 possible)
  - 8,760 hourly history_points  (one year of hourly snapshots)

Usage:
  python -m tests.fixtures.history_scale [--db-url postgresql+psycopg://...]

The generator runs outside pytest using direct asyncpg / psycopg connections via
SQLAlchemy to avoid polluting the rolled-back test session.  It TRUNCATES and
re-populates the fixture tables, so use only against the dedicated test database.

Environment variable: TEST_DATABASE_URL (default: postgresql+psycopg://audr:audr@localhost:5433/audr_test)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

_DEFAULT_DB_URL = "postgresql+psycopg://audr:audr@localhost:5433/audr_test"

WALLET_COUNT = 50
ASSET_COUNT = 100
HELD_PAIR_COUNT = 500
HOURLY_POINTS = 8_760  # 365 days × 24 h

_BASE_TIME = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
_BATCH_SIZE = 500


def _db_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", _DEFAULT_DB_URL)


async def generate(
    session: AsyncSession,
    *,
    hourly_points: int = HOURLY_POINTS,
    base_time: datetime = _BASE_TIME,
) -> dict[str, Any]:
    """Populate the scale fixture.  Returns timing stats.

    *hourly_points* exists so `scripts/benchmark.py` can run a reduced-scale
    pass on a host that cannot hold the full reference fixture (500 pairs ×
    8,760 snapshots = 4.38M valuation_line rows). Leave it at the default for
    any run whose numbers are published as reference results.

    *base_time* is the timestamp of the first (oldest) snapshot; points run
    forward hourly from there. The default is the fixed 2025-01-01 epoch that
    keeps SQL spot-checks reproducible. `scripts/benchmark.py` passes a
    now-anchored value instead, because `GET /api/v1/history` selects relative
    to wall-clock time and a fixture dated in the past returns zero entries
    for every period but `all`.
    """
    stats: dict[str, Any] = {}
    t0 = time.monotonic()

    wallets = await _generate_wallets(session)
    assets = await _generate_assets(session)
    pairs = _choose_pairs(wallets, assets)
    stats["wallets"] = len(wallets)
    stats["assets"] = len(assets)
    stats["pairs"] = len(pairs)

    # Build 8,760 hourly snapshots.
    snapshot_ids: list[uuid.UUID] = []
    ts_list = [base_time + timedelta(hours=h) for h in range(hourly_points)]

    snap_rows = [
        {
            "id": str(uuid.uuid4()),
            "ts": ts,
            "quality": "complete",
        }
        for ts in ts_list
    ]
    snapshot_ids = [uuid.UUID(r["id"]) for r in snap_rows]

    # valuation_snapshot.input_key (migration 0014) is NOT NULL UNIQUE, but it
    # arrived after this fixture did and the fixture has to work against a
    # database migrated to either side of it. Probing the column keeps the
    # generator independent of migration ordering. The snapshot id is a
    # trivially unique filler, which is exactly what 0014's own backfill uses.
    if await _has_column(session, "valuation_snapshot", "input_key"):
        for row in snap_rows:
            row["input_key"] = row["id"]
        insert_snapshot = """
            INSERT INTO valuation_snapshot
              (id, snapshotted_at, quality, published_at, input_key)
            VALUES (:id, :ts, :quality, :ts, :input_key)
        """
    else:
        insert_snapshot = """
            INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at)
            VALUES (:id, :ts, :quality, :ts)
        """

    await _batch_insert(session, insert_snapshot, snap_rows)

    t_snap = time.monotonic()
    stats["snapshot_insert_s"] = round(t_snap - t0, 2)
    logger.info("inserted %d snapshots in %.2fs", hourly_points, stats["snapshot_insert_s"])

    # Build valuation_lines: for each snapshot, include all held pairs.
    #
    # Server-side CROSS JOIN rather than client-side parameter rows. At the
    # reference scale this table holds 500 × 8,760 = 4.38M rows; materialising
    # that many bound-parameter dicts in Python cost ~1.7 GB of RSS and ran at
    # roughly 500 rows/s, which put a full-scale seed hours away and polluted
    # the benchmark's own peak-memory figure. Generating the rows in Postgres
    # keeps memory flat and the throughput disk-bound.
    line_count = await _generate_lines(session, snapshot_ids, pairs)

    t_lines = time.monotonic()
    stats["line_insert_s"] = round(t_lines - t_snap, 2)
    stats["line_count"] = line_count
    logger.info(
        "inserted %d valuation_lines in %.2fs",
        line_count,
        stats["line_insert_s"],
    )

    # Build history_points: one per snapshot, with aggregate totals.
    total_value_per_snap = Decimal(str(len(pairs))) * Decimal("1500000")
    hp_rows = [
        {
            "id": str(uuid.uuid4()),
            "sid": str(sid),
            "ts": base_time + timedelta(hours=i),
            "total": str(total_value_per_snap),
            "quality": "complete",
            "wc": WALLET_COUNT,
            "ac": ASSET_COUNT,
            "gap": False,
            "canonical": True,
        }
        for i, sid in enumerate(snapshot_ids)
    ]

    await _batch_insert(
        session,
        """
        INSERT INTO history_point
          (id, snapshot_id, snapshotted_at, total_value_usd, quality,
           included_wallet_count, included_asset_count, has_gap, is_canonical)
        VALUES (:id, :sid, :ts, :total, :quality, :wc, :ac, :gap, :canonical)
        """,
        hp_rows,
    )
    t_hp = time.monotonic()
    stats["history_point_insert_s"] = round(t_hp - t_lines, 2)
    logger.info(
        "inserted %d history_points in %.2fs",
        hourly_points,
        stats["history_point_insert_s"],
    )

    stats["hourly_points"] = hourly_points
    stats["first_point_at"] = ts_list[0].isoformat() if ts_list else None
    stats["last_point_at"] = ts_list[-1].isoformat() if ts_list else None
    stats["total_s"] = round(time.monotonic() - t0, 2)
    return stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _generate_wallets(session: AsyncSession) -> list[uuid.UUID]:
    rows = []
    ids = []
    for i in range(WALLET_COUNT):
        wid = uuid.uuid4()
        ids.append(wid)
        address = f"0x{i:040x}"
        rows.append({"id": str(wid), "addr": address, "label": f"wallet-{i}"})
    await _batch_insert(
        session,
        "INSERT INTO wallet (id, address, label, status) VALUES (:id, :addr, :label, 'active')",
        rows,
    )
    return ids


async def _generate_assets(session: AsyncSession) -> list[tuple[uuid.UUID, int]]:
    rows = []
    ids_dec: list[tuple[uuid.UUID, int]] = []
    for i in range(ASSET_COUNT):
        aid = uuid.uuid4()
        decimals = 18
        token_address = f"0x{(i + 1000):040x}"
        ids_dec.append((aid, decimals))
        rows.append(
            {
                "id": str(aid),
                "addr": token_address,
                "sym": f"TK{i}",
                "name": f"Token {i}",
                "dec": decimals,
            }
        )
    await _batch_insert(
        session,
        """
        INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
        VALUES (:id, :addr, :sym, :name, :dec, 'manual', false)
        """,
        rows,
    )
    return ids_dec


async def _has_column(session: AsyncSession, table: str, column: str) -> bool:
    result = await session.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.first() is not None


_LINE_SNAPSHOT_CHUNK = 200  # 200 snapshots × 500 pairs = 100k rows per statement


async def _generate_lines(
    session: AsyncSession,
    snapshot_ids: list[uuid.UUID],
    pairs: list[tuple[uuid.UUID, uuid.UUID, int]],
) -> int:
    """Insert one valuation_line per (snapshot, held pair), generated in Postgres.

    The held pairs go into a temporary table once; each chunk of snapshots is
    then one INSERT ... SELECT that cross-joins them. Returns the row count.
    """
    await session.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE _scale_pairs (
                wallet_id uuid NOT NULL,
                asset_id  uuid NOT NULL,
                decimals  smallint NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    await _batch_insert(
        session,
        "INSERT INTO _scale_pairs (wallet_id, asset_id, decimals) VALUES (:wid, :aid, :dec)",
        [
            {"wid": str(wallet_id), "aid": str(asset_id), "dec": decimals}
            for wallet_id, asset_id, decimals in pairs
        ],
    )

    # Every pair holds 1,000,000 whole units at $1.50 — the same values the
    # previous client-side generator produced, so published row totals and
    # per-snapshot portfolio totals are unchanged.
    for start in range(0, len(snapshot_ids), _LINE_SNAPSHOT_CHUNK):
        chunk = snapshot_ids[start : start + _LINE_SNAPSHOT_CHUNK]
        await session.execute(
            sa.text(
                """
                INSERT INTO valuation_line
                  (id, snapshot_id, wallet_id, asset_id, raw_amount,
                   block_number, price_usd, value_usd)
                SELECT
                    gen_random_uuid(),
                    s.id,
                    p.wallet_id,
                    p.asset_id,
                    1000000 * (10::numeric ^ p.decimals),
                    18000000,
                    1.50,
                    1500000
                FROM valuation_snapshot s
                CROSS JOIN _scale_pairs p
                WHERE s.id = ANY(:ids)
                """
            ),
            {"ids": [str(sid) for sid in chunk]},
        )

    return len(snapshot_ids) * len(pairs)


def _choose_pairs(
    wallets: list[uuid.UUID],
    assets: list[tuple[uuid.UUID, int]],
) -> list[tuple[uuid.UUID, uuid.UUID, int]]:
    """Return HELD_PAIR_COUNT unique (wallet_id, asset_id, decimals) tuples."""
    import hashlib

    pairs: list[tuple[uuid.UUID, uuid.UUID, int]] = []
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()

    # Deterministic pseudorandom selection via hash-based index.
    i = 0
    while len(pairs) < HELD_PAIR_COUNT:
        h = int(hashlib.md5(f"{i}".encode()).hexdigest(), 16)  # noqa: S324 — deterministic index derivation, not a security use of the hash
        w = wallets[h % WALLET_COUNT]
        a, dec = assets[(h // WALLET_COUNT) % ASSET_COUNT]
        if (w, a) not in seen:
            seen.add((w, a))
            pairs.append((w, a, dec))
        i += 1

    return pairs


async def _batch_insert(
    session: AsyncSession,
    stmt: str,
    rows: list[dict],
) -> None:
    for start in range(0, len(rows), _BATCH_SIZE):
        chunk = rows[start : start + _BATCH_SIZE]
        await session.execute(sa.text(stmt), chunk)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    engine = create_async_engine(_db_url(), echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            stats = await generate(session)
    await engine.dispose()
    print("Scale fixture generated:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    asyncio.run(_main())
