"""Alembic environment.

The URL comes from ``Settings``, never from ``alembic.ini``: one owner per
address. Autogenerate compares against the same ``MetaData`` the application
uses, so a hand-written migration cannot drift from the tables silently — the
drift check in CI runs ``alembic check``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from lab_assistant.adapters.db.tables import METADATA, embedding_table_for
from lab_assistant.config import load_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Embedding projections are per-dimension tables; declare the ones this
# deployment is expected to serve so they take part in autogenerate.
for dimension in (64, 384, 768, 1024):
    embedding_table_for(dimension)

target_metadata = METADATA
settings, _provenance = load_settings()


def run_migrations_offline() -> None:
    context.configure(
        url=settings.sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
