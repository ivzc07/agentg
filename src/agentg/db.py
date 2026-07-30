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
        def _foreign_keys_on(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine
