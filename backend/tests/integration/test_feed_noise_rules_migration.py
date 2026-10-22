"""Integration test for migration 0040 — feed-level keep/block noise rules
(AUD-655).

Runs the real migration chain against a disposable, dedicated database (same
discipline as test_aggregator_news_tier_migration.py and
test_release_news_noise_migration.py), because the aggregator-tier backfill
only touches rows 0039 seeds from live `asset`/`balance_observation` data.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud655"


def _alembic_config() -> Config:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return Config(str(candidate))
    raise RuntimeError("alembic.ini not found")


async def _recreate_scratch_db(admin_url: str) -> None:
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": _SCRATCH_DB},
        )
        await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"'))
        await conn.execute(sa.text(f'CREATE DATABASE "{_SCRATCH_DB}"'))
    await admin_engine.dispose()


async def _drop_scratch_db(admin_url: str) -> None:
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": _SCRATCH_DB},
        )
        await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"'))
    await admin_engine.dispose()


async def _seed_held_sushi(scratch_url: str) -> None:
    engine = create_async_engine(scratch_url)
    async with engine.begin() as conn:
        wallet_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO wallet (id, address_ciphertext, address_bidx,"
                " label_ciphertext, status)"
                " VALUES (:id, :ct, :bidx, '', 'active')"
            ),
            {"id": wallet_id, "ct": b"x" * 16, "bidx": b"y" * 32},
        )
        sushi_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                " VALUES (:id, :addr, 'SUSHI', 'SushiSwap', 18, 'manual')"
            ),
            {"id": sushi_id, "addr": "0x" + uuid.uuid4().hex[:40]},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO balance_observation"
                " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
                " VALUES (:id, :wallet, :asset, 1, 100, now())"
            ),
            {"id": uuid.uuid4(), "wallet": wallet_id, "asset": sushi_id},
        )
    await engine.dispose()


async def test_upgrade_sets_block_pattern_on_polygon_forum_feed() -> None:
    """The project-feed-scoped half of AUD-640 (opt-in by key, prerelease
    builds only) moves from `_RELEASE_SHAPED_PROJECT_FEEDS` in code to this
    feed's `block_pattern`."""
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            block_pattern = (
                await conn.execute(
                    sa.text(
                        "SELECT block_pattern FROM news_feed"
                        " WHERE key = 'project:polygon-forum-announcements'"
                    )
                )
            ).scalar_one()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    assert block_pattern is not None
    block_re = re.compile(block_pattern, re.IGNORECASE)
    assert block_re.search("Heimdall-v2 v0.12.2-beta-2 (Amoy)")
    assert not block_re.search("Bor v2.10.3 Release Mainnet and Testnet POS")
    assert not block_re.search("Security release review – Heimdall v0.12.1 (Lugano HF)")


async def test_upgrade_sets_block_pattern_on_aggregator_feeds() -> None:
    """AUD-654's `_EVERGREEN_EXPLAINER_RE` backstop moves to data here,
    expanded with the quote-spam and `price forecast` shapes measured live
    on the `aggregator` tier in AUD-655's wake comment."""
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        # Seed before 0039 runs (same discipline as
        # test_aggregator_news_tier_migration.py) — 0039 creates the feed
        # from live held-asset data, and 0040's backfill only touches rows
        # that already exist by the time it runs.
        await asyncio.to_thread(command.upgrade, cfg, "0038")
        await _seed_held_sushi(scratch_url)
        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            block_pattern = (
                await conn.execute(
                    sa.text("SELECT block_pattern FROM news_feed WHERE key = 'aggregator:sushi'")
                )
            ).scalar_one()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    assert block_pattern is not None
    block_re = re.compile(block_pattern, re.IGNORECASE)

    noise = [
        "What Is SushiSwap and How Does It Work? — Binance",
        "How Does Uniswap Work? A Complete Guide",
        "SushiSwap (SUSHI) Price Prediction 2026-2030",
        "ETH Price Forecast: Ethereum reclaims $2,500 as stocks rally - FXStreet",
        "Top Altcoins to Watch (2026)",
        "Convert 0.1 PENDLE (Pendle) to USD (United States Dollar) - Bybit",
        "Convert 100 LDO (LDO) to USD (United States Dollar) - Bybit",
        "Price of Decentraland (MANA) today in EUR – Quote and market trends - Bit2Me",
    ]
    for title in noise:
        assert block_re.search(title), f"expected to drop: {title!r}"

    real = [
        "SushiSwap exploit drains $3M from liquidity pool",
        "SushiSwap ships v3 router upgrade",
        "Binance lists new SushiSwap trading pair",
    ]
    for title in real:
        assert not block_re.search(title), f"expected to survive: {title!r}"


async def test_downgrade_drops_the_columns() -> None:
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.downgrade, cfg, "0039")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            columns = set(
                (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'news_feed'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    assert "keep_pattern" not in columns
    assert "block_pattern" not in columns
