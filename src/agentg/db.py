"""Database engine setup — one async engine per process."""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    if database_url.startswith("sqlite"):
        # SQLite ships with FK enforcement OFF; production (Postgres) always
        # enforces. Turn it on so the test fixtures surface dangling-reference
        # bugs (e.g. a forget-me that misses a FK) instead of passing silently.
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL mode allows concurrent reads and a single concurrent
            # writer without "database is locked" errors — required for
            # the barrier-based concurrent tests (issue #212, fix-r11).
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    # Per-turn SQL-statement counter (issue #161).
    from agentg.instrument import register_sql_counter

    register_sql_counter(engine)

    return engine
