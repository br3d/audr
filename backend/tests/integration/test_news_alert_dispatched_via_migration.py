"""Migration test for 0034: news_alert.dispatched_via (AUD-595).

Same discipline as `test_news_alert_migration.py` — the real chain against a
disposable, dedicated database — because the thing being asserted is the
artefact itself. AUD-595 is a ticket about the database telling the truth, so
"the column exists, it is nullable, and it refuses a road that is not a road"
is the acceptance criterion and not an implementation detail.

Nullability is asserted deliberately: rows dispatched before 0034 cannot be
reclassified, and a NOT NULL with a default would have written a road over
history that nobody can vouch for.

THE DOWNGRADE IS RUN, NOT ASSUMED, and that is the half that earned this file.
`audr.models`' naming convention is `ck_%(table_name)s_%(constraint_name)s` and
alembic applies it to `op.create_check_constraint`/`op.drop_constraint` as well,
so passing the already-prefixed name produced `ck_news_alert_ck_news_alert_
dispatched_via` and a `downgrade()` that died with `UndefinedObject`. Nothing in
the suite executed a downgrade past 0034, so that reached `main` green — and a
deploy that migrates and then fails is exactly when the rollback is needed
(deploy-runbook.md, "A rollback must restore the schema").
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud595"


def _alembic_config() -> Config:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return Config(str(candidate))
    raise RuntimeError("alembic.ini not found")


async def _reset_scratch_db(admin_url: str, *, create: bool) -> None:
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
        if create:
            await conn.execute(sa.text(f'CREATE DATABASE "{_SCRATCH_DB}"'))
    await admin_engine.dispose()


async def test_0034_upgrade_and_downgrade() -> None:
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _reset_scratch_db(base_url, create=True)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0033")
        scratch_engine = create_async_engine(scratch_url)

        async with scratch_engine.connect() as conn:
            assert await _column(conn, "dispatched_via") is None, "0033 must not have the column"

        await asyncio.to_thread(command.upgrade, cfg, "0034")

        async with scratch_engine.connect() as conn:
            column = await _column(conn, "dispatched_via")
            assert column is not None
            data_type, is_nullable, default = column
            assert data_type == "text"
            # NULL means "road not recorded". A default would invent a road for
            # every pre-0034 row and for every caller that does not pass --via.
            assert is_nullable == "YES"
            assert default is None

            # Found by DEFINITION, not by name, on purpose: the naming
            # convention rewrites whatever a migration passes (0033's three
            # checks are doubled on the stand for the same reason), so pinning
            # either spelling would only pin whichever one was measured. What
            # must hold is that a CHECK on this column exists and carries the
            # whole road vocabulary; that the name is self-consistent is proven
            # by the downgrade below actually running.
            checks = [
                row[0]
                for row in (
                    await conn.execute(
                        sa.text(
                            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c"
                            " JOIN pg_class t ON t.oid = c.conrelid"
                            " WHERE t.relname = 'news_alert' AND c.contype = 'c'"
                        )
                    )
                ).all()
            ]
            road_checks = [c for c in checks if "dispatched_via" in c]
            assert len(road_checks) == 1, checks
            for road in ("board", "telegram", "board+telegram", "spool"):
                assert road in road_checks[0], road_checks[0]

        # And the constraint actually bites: `alerts_mark --via` constrains the
        # same vocabulary in argparse, but the row is the artefact, so it has to
        # be the thing that cannot hold a road that is not a road.
        async with scratch_engine.begin() as conn:
            await _seed_alert(conn, via="spool")
        async with scratch_engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await _seed_alert(conn, via="carrier-pigeon")

        await asyncio.to_thread(command.downgrade, cfg, "0033")

        async with scratch_engine.connect() as conn:
            assert await _column(conn, "dispatched_via") is None

        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _reset_scratch_db(base_url, create=False)


async def _column(conn: object, name: str) -> tuple[str, str, str | None] | None:
    """(data_type, is_nullable, column_default) for a news_alert column, or None."""
    row = (
        await conn.execute(  # type: ignore[attr-defined]
            sa.text(
                "SELECT data_type, is_nullable, column_default"
                " FROM information_schema.columns"
                " WHERE table_name = 'news_alert' AND column_name = :name"
            ),
            {"name": name},
        )
    ).first()
    return None if row is None else (row[0], row[1], row[2])


async def _seed_alert(conn: object, *, via: str) -> None:
    """One minimal alert row plus the two rows its foreign keys need."""
    asset_id = uuid.uuid4()
    news_id = uuid.uuid4()
    await conn.execute(  # type: ignore[attr-defined]
        sa.text(
            "INSERT INTO asset (id, token_address, symbol, name, decimals, source)"
            " VALUES (:id, :address, :symbol, 'Probe asset', 18, 'manual')"
        ),
        {
            "id": str(asset_id),
            # ck_asset_address_lower: lowercase, or the insert fails for a
            # reason that has nothing to do with this test.
            "address": f"0x{asset_id.hex}",
            "symbol": f"P{asset_id.hex[:6].upper()}",
        },
    )
    await conn.execute(  # type: ignore[attr-defined]
        sa.text(
            "INSERT INTO asset_news"
            " (id, asset_id, source, external_id, title, url, news_site, published_at)"
            " VALUES (:id, :asset_id, 'manual', :external_id, 'Probe headline',"
            "         :url, 'example.test', now())"
        ),
        {
            "id": str(news_id),
            "asset_id": str(asset_id),
            "external_id": str(news_id),
            "url": f"https://example.test/{news_id}",
        },
    )
    await conn.execute(  # type: ignore[attr-defined]
        sa.text(
            "INSERT INTO news_alert"
            " (asset_news_id, asset_id, category, severity, summary, classifier,"
            "  dispatched_at, dispatched_via)"
            " VALUES (:news_id, :asset_id, 'other', 'critical', 'probe', 'heuristic',"
            "         now(), :via)"
        ),
        {"news_id": str(news_id), "asset_id": str(asset_id), "via": via},
    )
