"""Engine construction and schema creation.

The database URL has exactly one owner: ``Settings.database_url``, defaulted for
the ``local`` scenario and injected by the compose file otherwise. Nothing else in
the codebase builds a connection string.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lab_assistant.adapters.db.repositories import sql_uow_factory
from lab_assistant.adapters.db.search import PostgresSearchIndex
from lab_assistant.adapters.db.tables import METADATA, embedding_table_for
from lab_assistant.config import Settings

__all__ = ["create_engine_and_factory", "create_schema", "make_engine"]


def make_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the configured database.

    SQLite uses a static pool and accepts no sizing arguments; PostgreSQL gets the
    pool size and a statement timeout, so a runaway query cannot hold a
    connection indefinitely.
    """
    if settings.database_url.startswith("sqlite"):
        return create_async_engine(settings.database_url, future=True)
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        connect_args={
            "server_settings": {
                "application_name": "lab-ai-assistant",
                "statement_timeout": str(settings.database_statement_timeout_ms),
            }
        },
    )


def create_engine_and_factory(settings: Settings):
    """Return ``(engine, uow_factory, search_index)`` for the configured database."""
    engine = make_engine(settings)
    return engine, sql_uow_factory(engine), PostgresSearchIndex(engine)


async def create_schema(
    engine: AsyncEngine, *, dimensions: tuple[int, ...] = (64, 384, 768)
) -> None:
    """Create every table, including the per-dimension embedding projections.

    Used by the offline SQLite suite and by ``lab db create`` for a scratch
    database. Production schema changes go through Alembic.
    """
    for dimension in dimensions:
        embedding_table_for(dimension)
    async with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            from sqlalchemy import text

            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(METADATA.create_all)
