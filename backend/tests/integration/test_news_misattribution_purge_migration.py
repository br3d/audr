"""Integration test for migration 0027 — purging misattributed news (AUD-503).

Runs the real migration chain against a disposable, dedicated database
(created and dropped by this test) rather than the shared test database, so a
DELETE that is wider than intended cannot take other tests' news rows with it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud503"


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


_INSERT_ASSET = sa.text(
    """
    INSERT INTO asset (id, token_address, symbol, name, decimals, source)
    VALUES (:id, :addr, :sym, :name, 18, 'manual')
    """
)

_INSERT_NEWS = sa.text(
    """
    INSERT INTO asset_news
      (asset_id, source, external_id, title, url, news_site, published_at)
    VALUES
      (:asset_id, 'press:coindesk', :external_id, :title,
       'https://example.com/' || :external_id, 'Example', now())
    """
)


async def test_upgrade_0027_drops_only_untracked_assets_news() -> None:
    """0027 must delete the pre-allow-list residue and nothing else.

    Upgrades a fresh database to 0026 (which creates `news_tracked_asset` and
    seeds WETH/UNI/AAVE/LINK), writes news the way the pre-fix matcher did —
    one row on an allow-listed holding, one on the meme token ticked 'BITCOIN'
    that absorbed 44 staging rows, one on a token literally named 'ASSET' —
    then upgrades to head and checks the allow-listed row survived while both
    misattributed rows are gone.
    """
    base_url = os.environ["DATABASE_URL"]
    admin_url = base_url
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(admin_url)

    cfg = _alembic_config()
    tracked_id = uuid.uuid4()
    meme_id = uuid.uuid4()
    word_id = uuid.uuid4()

    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0026")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            for asset_id, symbol, name in (
                (tracked_id, "WETH", "Wrapped Ether"),
                (meme_id, "BITCOIN", "HarryPotterObamaSonic10Inu"),
                (word_id, "ASSET", "Asset Token"),
            ):
                await conn.execute(
                    _INSERT_ASSET,
                    {
                        "id": asset_id,
                        "addr": "0x" + uuid.uuid4().hex[:40],
                        "sym": symbol,
                        "name": name,
                    },
                )
                await conn.execute(
                    _INSERT_NEWS,
                    {
                        "asset_id": asset_id,
                        "external_id": f"article-{symbol.lower()}",
                        "title": "Bitcoin rallies as the asset class matures",
                    },
                )
            await conn.commit()
        await scratch_engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            surviving = set(
                (await conn.execute(sa.text("SELECT asset_id::text FROM asset_news")))
                .scalars()
                .all()
            )
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(admin_url)

    assert surviving == {str(tracked_id)}
