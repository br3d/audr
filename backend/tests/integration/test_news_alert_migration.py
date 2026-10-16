"""Migration tests for 0033 (ADR-007 phase 1, AUD-583).

Runs the real migration chain against a disposable, dedicated database (same
discipline as `test_news_coverage_seed_migration.py`) so upgrade/downgrade
can be asserted without the shared test database's fixtures interfering.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud583"


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


async def test_0033_upgrade_and_downgrade() -> None:
    base_url = os.environ["DATABASE_URL"]
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(base_url)

    cfg = _alembic_config()
    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0032")

        scratch_engine = create_async_engine(scratch_url)

        # Pre-0033: neither analyzed_at nor news_alert exist yet.
        async with scratch_engine.connect() as conn:
            cols_before = {
                row[0]
                for row in (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'asset_news'"
                        )
                    )
                ).all()
            }
            assert "analyzed_at" not in cols_before
            table_exists_before = (
                await conn.execute(
                    sa.text(
                        "SELECT 1 FROM information_schema.tables WHERE table_name = 'news_alert'"
                    )
                )
            ).first()
            assert table_exists_before is None

        await asyncio.to_thread(command.upgrade, cfg, "0033")

        async with scratch_engine.connect() as conn:
            cols_after = {
                row[0]
                for row in (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'asset_news'"
                        )
                    )
                ).all()
            }
            assert "analyzed_at" in cols_after

            alert_cols = {
                row[0]
                for row in (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'news_alert'"
                        )
                    )
                ).all()
            }
            assert alert_cols == {
                "id",
                "asset_news_id",
                "asset_id",
                "category",
                "severity",
                "summary",
                "classifier",
                "model",
                "created_at",
                "read_at",
                "dispatched_at",
                "dispatch_error",
            }

            indexes = {
                row[0]
                for row in (
                    await conn.execute(
                        sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'news_alert'")
                    )
                ).all()
            }
            assert "ix_news_alert_unread" in indexes
            assert "ix_news_alert_pending" in indexes

            # ck_integration_kind must be untouched by this migration (AUD-583
            # amendment — 'anthropic' is phase 3, not phase 1).
            all_integration_constraints = (
                await conn.execute(
                    sa.text(
                        "SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c"
                        " JOIN pg_class t ON t.oid = c.conrelid"
                        " WHERE t.relname = 'integration'"
                    )
                )
            ).all()
            integration_kind_defs = [d for n, d in all_integration_constraints if "kind" in n]
            assert integration_kind_defs, all_integration_constraints
            assert all("anthropic" not in d for d in integration_kind_defs)

            news_alert_constraints = (
                await conn.execute(
                    sa.text(
                        "SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c"
                        " JOIN pg_class t ON t.oid = c.conrelid"
                        " WHERE t.relname = 'news_alert'"
                    )
                )
            ).all()
            category_defs = [d for n, d in news_alert_constraints if "category" in n]
            assert category_defs, news_alert_constraints
            category_check = category_defs[0]
            for category in (
                "hack_exploit",
                "depeg",
                "delisting",
                "regulatory",
                "market_move",
                "other",
            ):
                assert category in category_check

        await asyncio.to_thread(command.downgrade, cfg, "0032")

        async with scratch_engine.connect() as conn:
            cols_rolled_back = {
                row[0]
                for row in (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'asset_news'"
                        )
                    )
                ).all()
            }
            assert "analyzed_at" not in cols_rolled_back
            table_rolled_back = (
                await conn.execute(
                    sa.text(
                        "SELECT 1 FROM information_schema.tables WHERE table_name = 'news_alert'"
                    )
                )
            ).first()
            assert table_rolled_back is None

        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(base_url)
