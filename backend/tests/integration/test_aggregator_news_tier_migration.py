"""Integration test for migration 0039 — the aggregator news tier (AUD-654).

Runs the real migration chain against a disposable, dedicated database (same
discipline as test_release_news_noise_migration.py), because this migration's
seed is read from live `asset`/`balance_observation` rows at migration time —
assertions here must not be able to see a shared test database's unrelated
fixtures.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud654"


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


async def test_upgrade_seeds_one_aggregator_feed_per_held_symbol() -> None:
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0038")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.begin() as conn:
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
            # A held asset with a zero balance is not "held" (AUD-447's
            # exclusion carve-out is a different flag entirely) and must not
            # get an aggregator row.
            unheld_id = uuid.uuid4()
            await conn.execute(
                sa.text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                    " VALUES (:id, :addr, 'DUST', 'Dust Token', 18, 'manual')"
                ),
                {"id": unheld_id, "addr": "0x" + uuid.uuid4().hex[:40]},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO balance_observation"
                    " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
                    " VALUES (:id, :wallet, :asset, 0, 100, now())"
                ),
                {"id": uuid.uuid4(), "wallet": wallet_id, "asset": unheld_id},
            )
        await scratch_engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT nf.key, nf.tier, nf.url, nfs.asset_symbol FROM news_feed nf"
                        " JOIN news_feed_symbol nfs ON nfs.feed_id = nf.id"
                        " WHERE nf.tier = 'aggregator'"
                    )
                )
            ).all()
            allowed_tiers = (
                await conn.execute(
                    sa.text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                        " WHERE conname = 'ck_news_feed_tier'"
                    )
                )
            ).scalar_one()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    assert [r for r in rows] == [
        (
            "aggregator:sushi",
            "aggregator",
            f"https://news.google.com/rss/search?q=%22{quote('SushiSwap', safe='')}%22+crypto"
            "&hl=en-US&gl=US&ceid=US:en",
            "SUSHI",
        )
    ]
    assert "aggregator" in allowed_tiers


async def test_upgrade_is_idempotent_against_a_hand_seeded_feed() -> None:
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0038")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.begin() as conn:
            wallet_id = uuid.uuid4()
            await conn.execute(
                sa.text(
                    "INSERT INTO wallet (id, address_ciphertext, address_bidx,"
                    " label_ciphertext, status)"
                    " VALUES (:id, :ct, :bidx, '', 'active')"
                ),
                {"id": wallet_id, "ct": b"x" * 16, "bidx": b"y" * 32},
            )
            ens_id = uuid.uuid4()
            await conn.execute(
                sa.text(
                    "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
                    " VALUES (:id, :addr, 'ENS', 'Ethereum Name Service', 18, 'manual')"
                ),
                {"id": ens_id, "addr": "0x" + uuid.uuid4().hex[:40]},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO balance_observation"
                    " (id, wallet_id, asset_id, raw_amount, block_number, observed_at)"
                    " VALUES (:id, :wallet, :asset, 1, 100, now())"
                ),
                {"id": uuid.uuid4(), "wallet": wallet_id, "asset": ens_id},
            )
            # Operator hand-seeded this key before the migration ran on this
            # install — must not be duplicated, same discipline as 0031/0038.
            feed_id = uuid.uuid4()
            await conn.execute(
                sa.text(
                    "INSERT INTO news_feed (id, key, tier, url)"
                    " VALUES (:id, 'aggregator:ens', 'press', 'https://example.com/hand-seeded')"
                ),
                {"id": feed_id},
            )
        await scratch_engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT nf.id, nf.url, nf.tier, nfs.asset_symbol FROM news_feed nf"
                        " JOIN news_feed_symbol nfs ON nfs.feed_id = nf.id"
                        " WHERE nf.key = 'aggregator:ens'"
                    )
                )
            ).all()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    # One row, the hand-seeded id and url untouched — only the symbol mapping
    # is new.
    assert len(rows) == 1
    assert rows[0][0] == feed_id
    assert rows[0][1] == "https://example.com/hand-seeded"
    assert rows[0][2] == "press"
    assert rows[0][3] == "ENS"


async def test_downgrade_removes_aggregator_feeds_and_tier() -> None:
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0038")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.begin() as conn:
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
        await scratch_engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.downgrade, cfg, "0038")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            remaining = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM news_feed WHERE tier = 'aggregator'")
                )
            ).scalar_one()
        await scratch_engine.dispose()

        # The tier check constraint must have reverted too — a fresh
        # connection, since the failed INSERT below leaves its own
        # transaction unusable.
        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            with pytest.raises(sa.exc.DBAPIError, match="ck_news_feed_tier"):
                await conn.execute(
                    sa.text(
                        "INSERT INTO news_feed (id, key, tier, url)"
                        " VALUES (:id, 'aggregator:x', 'aggregator', 'https://example.com')"
                    ),
                    {"id": uuid.uuid4()},
                )
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    assert remaining == 0
