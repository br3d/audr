"""Alembic migration environment — async SQLAlchemy with psycopg."""

import asyncio
import logging
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

from audr.models import metadata

alembic_config = context.config

if alembic_config.config_file_name is not None:
    # disable_existing_loggers=False matters whenever alembic runs in the same
    # process as the app or the test suite: the default (True) permanently sets
    # .disabled on every logger created before this line, so every `audr.*`
    # logger already imported goes silent for the rest of the process. In the
    # integration suite that silently broke caplog assertions in any test file
    # ordered after the migration tests (AUD-558); in-process it would discard
    # app logs after a migration.
    fileConfig(alembic_config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata

logger = logging.getLogger("alembic.env")


def run_migrations_offline() -> None:
    url = os.environ["DATABASE_URL"]
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    ini_section = alembic_config.get_section(alembic_config.config_ini_section, {})
    ini_section["sqlalchemy.url"] = os.environ["DATABASE_URL"]
    connectable = async_engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
