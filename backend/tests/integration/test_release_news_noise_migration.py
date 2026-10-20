"""Integration test for migrations 0037 + 0038 — release tier (AUD-640).

Asserts the end state of the chain, not 0037 alone: 0037 disabled two
noise-only feeds and filtered noise-shaped titles on the rest, then 0038
retired the tier outright, so every `release:*` row goes and the two rows
0037 kept (`v2.10.3`, `v2.10.1-hotfix2`) are exactly the ones the founder
reopened the issue about. The test keeps them in the fixture for that reason.

Runs the real migration chain against a disposable, dedicated database
(created and dropped by this test) rather than the shared test database, so a
DELETE that is wider than intended cannot take other tests' news rows with it
— same pattern as test_news_misattribution_purge_migration.py.
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

_SCRATCH_DB = "audr_migration_scratch_aud640"


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
      (:asset_id, :source, :external_id, :title,
       'https://example.com/' || :external_id, :source, now())
    """
)

# (source, external_id, title, survives)
_ROWS: list[tuple[str, str, str, bool]] = [
    # The whole release tier goes — noise-shaped or not, feed disabled by
    # 0037 or retired by 0038.
    ("release:ensjs", "ensjs-1", "@ensdomains/ensjs@5.2.4", False),
    ("release:ensjs", "ensjs-2", "ENS JS client 5.2", False),
    ("release:kyberswap-interface", "ksi-1", "KyberSwap Interface - v7.11.10", False),
    ("release:polygon-bor", "bor-1", "v2.10.2-beta5", False),
    ("release:polygon-bor", "bor-2", "v2.10.2-preconf (Testing Only)", False),
    # The two 0037 kept and the founder reopened AUD-640 about.
    ("release:polygon-bor", "bor-3", "v2.10.3", False),
    ("release:polygon-bor", "bor-4", "v2.10.1-hotfix2", False),
    ("release:go-ethereum", "geth-1", "Protein Sludge Pack (v1.17.8)", False),
    # Non-release tiers are untouched, including a blog post about a beta.
    ("project:ens-blog", "blog-1", "Our beta is live", True),
    ("press:coindesk", "press-1", "Alpha leak hits markets", True),
]


async def test_release_tier_is_retired_and_matic_keeps_a_source() -> None:
    base_url = os.environ["DATABASE_URL"]
    admin_url = base_url
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(admin_url)

    cfg = _alembic_config()
    asset_id = uuid.uuid4()

    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0036")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            await conn.execute(
                _INSERT_ASSET,
                {
                    "id": asset_id,
                    "addr": "0x" + uuid.uuid4().hex[:40],
                    "sym": "ENS",
                    "name": "Ethereum Name Service",
                },
            )
            for source, external_id, title, _survives in _ROWS:
                await conn.execute(
                    _INSERT_NEWS,
                    {
                        "asset_id": asset_id,
                        "source": source,
                        "external_id": external_id,
                        "title": title,
                    },
                )
            await conn.commit()
        await scratch_engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            surviving = set(
                (await conn.execute(sa.text("SELECT external_id FROM asset_news"))).scalars().all()
            )
            disabled = set(
                (await conn.execute(sa.text("SELECT key FROM news_feed WHERE NOT enabled")))
                .scalars()
                .all()
            )
            enabled_release = (
                await conn.execute(
                    sa.text("SELECT count(*) FROM news_feed WHERE enabled AND tier = 'release'")
                )
            ).scalar_one()
            # MATIC's only feed was release-tier; 0038 must not leave it bare.
            matic_feeds = set(
                (
                    await conn.execute(
                        sa.text(
                            """
                            SELECT nf.key FROM news_feed nf
                            JOIN news_feed_symbol nfs ON nfs.feed_id = nf.id
                            WHERE nf.enabled AND nfs.asset_symbol = 'MATIC'
                            """
                        )
                    )
                )
                .scalars()
                .all()
            )
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(admin_url)

    assert surviving == {external_id for _s, external_id, _t, survives in _ROWS if survives}
    assert enabled_release == 0
    assert {"release:ensjs", "release:kyberswap-interface", "release:polygon-bor"} <= disabled
    assert matic_feeds == {"project:polygon-forum-announcements"}
