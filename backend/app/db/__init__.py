"""Database initialisation for the karaoke application (PostgreSQL).

Provides ``init_pg()``, an async function that:
1. Creates an asyncpg connection pool.
2. Executes ``init_pg.sql`` to create tables, indexes, triggers, and FTS.
3. Returns the pool for the caller to store and reuse.

Typical usage (inside FastAPI lifespan):

    pool = await init_pg(settings.pg_dsn)
    yield
    await pool.close()
"""

import pathlib

import asyncpg
import structlog

logger = structlog.get_logger(__name__)

# Locate init_pg.sql relative to this file.
_INIT_SQL_PATH = pathlib.Path(__file__).parent / "init_pg.sql"


async def init_pg(dsn: str) -> asyncpg.Pool:
    """Create a connection pool and apply the PostgreSQL schema.

    Args:
        dsn: PostgreSQL connection string, e.g.
             ``postgresql://karaoke:karaoke@postgres:5432/karaoke``.

    Returns:
        An open ``asyncpg.Pool`` with the full schema applied.
    """
    logger.info("opening_pg_pool", dsn=dsn.split("@")[-1])  # log host only

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

    schema_sql = _INIT_SQL_PATH.read_text(encoding="utf-8")

    async with pool.acquire() as conn:
        await conn.execute(schema_sql)

    logger.info("pg_schema_applied")

    return pool
