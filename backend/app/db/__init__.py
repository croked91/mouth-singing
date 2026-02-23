"""Database initialisation for the karaoke application.

Provides ``init_db()``, an async function that:
1. Creates all parent directories for the SQLite file.
2. Opens an aiosqlite connection.
3. Enables WAL journal mode for better concurrent read performance.
4. Executes ``init.sql`` to create tables, indexes, triggers, and the FTS5
   virtual table — all idempotent (IF NOT EXISTS).
5. Returns the open connection for the caller to store and reuse.

Typical usage (inside FastAPI lifespan):

    db = await init_db(settings.database_url)
    yield
    await db.close()
"""

import pathlib

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

# Locate init.sql relative to this file so the path works regardless of cwd.
_INIT_SQL_PATH = pathlib.Path(__file__).parent / "init.sql"


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and apply the schema.

    Args:
        db_path: Absolute path to the SQLite database file, e.g.
                 ``/data/sqlite/karaoke.db``.

    Returns:
        An open ``aiosqlite.Connection`` with WAL mode enabled and the full
        schema applied.
    """
    path = pathlib.Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("opening_sqlite_database", path=str(path))

    connection = await aiosqlite.connect(str(path))

    # Row factory makes rows behave like dicts — very handy for API responses.
    connection.row_factory = aiosqlite.Row

    # WAL mode allows concurrent readers while a writer is active.
    await connection.execute("PRAGMA journal_mode=WAL")
    await connection.execute("PRAGMA foreign_keys=OFF")  # intentional per ADR-03

    schema_sql = _INIT_SQL_PATH.read_text(encoding="utf-8")
    await connection.executescript(schema_sql)
    await connection.commit()

    logger.info("sqlite_schema_applied", path=str(path))

    return connection
