"""Integration tests for the portfolio news-coverage seeds (AUD-568 part B).

Covers migrations 0031, 0032 and 0035 together: 0032 only exists because 0031's
two ENS sources both turned out to sit outside the retention window, and 0035
exists because 0024's two ETH sources publish weekly, so the set is one running
coverage decision and asserting the migrations separately would let a later edit
drop a half without a red test.

Runs the real migration chain against a disposable, dedicated database (same
discipline as `test_news_misattribution_purge_migration.py`) so the seeded
registry rows can be asserted exactly, without the shared test database's
fixtures deleting them underneath the assertions.

The idempotency half matters in practice, not in theory: the stand had rows
hand-inserted into these registries while AUD-568 part A was being verified,
so 0031 has to land on a partially-populated database without duplicating a
feed, a space or an allow-list entry.
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

_SCRATCH_DB = "audr_migration_scratch_aud568"

_EXPECTED_FEEDS = {
    "project:kyberswap-blog": ("project", {"KNC"}),
    "release:kyberswap-interface": ("release", {"KNC"}),
    "release:ens-contracts": ("release", {"ENS"}),
    "project:lido-blog": ("project", {"LDO"}),
    "project:curve-news": ("project", {"CRV"}),
    "release:polygon-bor": ("release", {"MATIC"}),
    # 0032 — ENS's live sources; 0031's two were both outside retention.
    "project:ens-blog": ("project", {"ENS"}),
    "release:ensjs": ("release", {"ENS"}),
    # 0035 — ETH's daily sources; 0024's two publish about weekly (AUD-623).
    "project:ethresearch": ("project", {"ETH", "WETH"}),
    "project:ethereum-magicians": ("project", {"ETH", "WETH"}),
    # 0038 — MATIC's replacement for `release:polygon-bor`, which was its only
    # feed before the release tier was retired (AUD-640).
    "project:polygon-forum-announcements": ("project", {"MATIC"}),
}

# Disabled along the way, so the rows above are "seeded", not "polled":
# 0037 retired two developer-artefact feeds, 0038 the rest of the tier.
_EXPECTED_DISABLED = {
    "release:kyberswap-interface",
    "release:ens-contracts",
    "release:polygon-bor",
    "release:ensjs",
}

_EXPECTED_SPACES = {("ENS", "ens.eth"), ("LDO", "lido-snapshot.eth"), ("SUSHI", "sushigov.eth")}

# Seeded by 0026. 0031 must not widen the press/GDELT allow-list: every
# symbol it covers gets exact mapping-based attribution instead, per the
# preference order in docs/news-sources.md §9 (AUD-515).
_SEED_TRACKED = {"WETH", "UNI", "AAVE", "LINK"}


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


async def test_upgrade_seeds_news_coverage_idempotently() -> None:
    """0031/0032 seed every verified source once, on top of hand-inserted rows."""
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0030")

        scratch_engine = create_async_engine(scratch_url)
        # The partial hand-applied state 0031 must tolerate: one of its feeds
        # already present (with its symbol mapped), one of its spaces, one of
        # its allow-list symbols.
        async with scratch_engine.begin() as conn:
            feed_id = uuid.uuid4()
            await conn.execute(
                sa.text(
                    "INSERT INTO news_feed (id, key, tier, url) "
                    "VALUES (:id, 'project:lido-blog', 'project', 'https://blog.lido.fi/rss/')"
                ),
                {"id": feed_id},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO news_feed_symbol (feed_id, asset_symbol) VALUES (:feed_id, 'LDO')"
                ),
                {"feed_id": feed_id},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO snapshot_space (id, asset_symbol, space_id) "
                    "VALUES (:id, 'ENS', 'ens.eth')"
                ),
                {"id": uuid.uuid4()},
            )
            await conn.execute(
                sa.text("INSERT INTO news_tracked_asset (id, asset_symbol) VALUES (:id, 'KNC')"),
                {"id": uuid.uuid4()},
            )

        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with scratch_engine.connect() as conn:
            feed_rows = (
                await conn.execute(
                    sa.text(
                        "SELECT f.key, f.tier, s.asset_symbol FROM news_feed f "
                        "LEFT JOIN news_feed_symbol s ON s.feed_id = f.id"
                    )
                )
            ).all()
            space_rows = (
                await conn.execute(sa.text("SELECT asset_symbol, space_id FROM snapshot_space"))
            ).all()
            tracked_rows = (
                await conn.execute(sa.text("SELECT asset_symbol FROM news_tracked_asset"))
            ).all()
            disabled_keys = set(
                (await conn.execute(sa.text("SELECT key FROM news_feed WHERE NOT enabled")))
                .scalars()
                .all()
            )
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)

    # No duplicate feed row for the hand-inserted key, and every new feed
    # mapped to exactly the symbols the seeds claim.
    keys = [key for key, _tier, _symbol in feed_rows]
    for key, (tier, symbols) in _EXPECTED_FEEDS.items():
        matching = [(t, s) for k, t, s in feed_rows if k == key]
        assert len(matching) == len(symbols), f"{key}: {matching}"
        assert {t for t, _s in matching} == {tier}
        assert {s for _t, s in matching} == symbols
    assert keys.count("project:lido-blog") == 1
    assert _EXPECTED_DISABLED <= disabled_keys
    assert not (disabled_keys & (set(_EXPECTED_FEEDS) - _EXPECTED_DISABLED))

    spaces = [(symbol, space) for symbol, space in space_rows]
    assert _EXPECTED_SPACES <= set(spaces)
    assert spaces.count(("ENS", "ens.eth")) == 1
    # A Snapshot space whose newest proposal predates the 90-day retention
    # window can only ever insert zero rows — the seed deliberately leaves
    # `curve.eth` out, so a later edit that adds it has to say why.
    assert not [s for _sym, s in spaces if s == "curve.eth"]

    tracked = [symbol for (symbol,) in tracked_rows]
    # 'KNC' is only here because this test hand-inserted it above, standing in
    # for a row an operator added; 0031 adds none of its own and must not
    # duplicate one it finds.
    assert set(tracked) == _SEED_TRACKED | {"KNC"}
    assert tracked.count("KNC") == 1


@pytest.mark.parametrize("key", sorted(_EXPECTED_FEEDS))
def test_seeded_feed_urls_are_registry_shaped(key: str) -> None:
    """Keys follow the `tier:slug` convention `_targets_for_feed` relies on.

    Cheap guard rather than a live HTTP check: every seeded URL was verified
    against the real host before it landed, but a test must not depend on
    eight third-party endpoints being up.
    """
    tier, _symbols = _EXPECTED_FEEDS[key]
    assert key.startswith(f"{tier}:")
    assert key.count(":") == 1
