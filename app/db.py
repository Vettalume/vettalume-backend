from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import Session, sessionmaker, DeclarativeBase

from .config import settings

# SQLite needs a shared connection so in-memory tests keep their tables across sessions.
connect_args: dict = {}
engine_kwargs: dict = {}
if settings.database_url.startswith("sqlite"):
    from sqlalchemy.pool import StaticPool

    connect_args = {"check_same_thread": False}
    engine_kwargs = {"poolclass": StaticPool}
else:
    # Postgres / any server DB: a real bounded QueuePool. These bound how many connections EACH
    # worker process holds; total at peak ~= (workers) x (pool_size + max_overflow). That must stay
    # under Postgres max_connections (raise it, or run pgbouncer) when scaling workers wide.
    engine_kwargs = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
    }
    # Keep pooled connections warm against a managed/remote Postgres (e.g. Neon), which drops idle
    # connections aggressively. Without TCP keepalives every request re-does the ~350ms TLS+auth
    # handshake instead of reusing a ~80ms warm connection. (libpq/psycopg2 keepalive params.)
    if "psycopg2" in settings.database_url or settings.database_url.startswith("postgresql"):
        connect_args = {
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        }

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    pool_pre_ping=settings.db_pool_pre_ping,
    future=True,
    **engine_kwargs,
)
# Dev/prod parity: make SQLite enforce foreign keys the way Postgres always does, so FK-ordering
# bugs (e.g. inserting a child before its parent) fail loudly in tests instead of silently passing
# on SQLite and then exploding on Postgres in production.
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_fk_on(dbapi_connection, _record):  # pragma: no cover
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Phase-0 schema bootstrap. Dev-only: real migrations move to Alembic once the schema
    stabilises (Phase 1). create_all is intentionally chosen here so the skeleton runs with
    zero migration friction."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)


def bulk_insert(db: Session, model, rows: list[dict], *, batch_size: int = 500) -> int:
    """Insert many rows fast: each batch is one round trip (executemany), then a single commit.

    Over a remote DB (Neon) this is the difference between minutes and seconds — `db.add()` per row
    pays ~one network round trip each; batching sends up to `batch_size` rows per trip.

        from app.db import bulk_insert
        bulk_insert(db, models.Item, [{"id": ..., "stem": ...}, ...])

    `rows` is a list of plain dicts (column name -> value). Returns the number of rows inserted.
    For upserts / dedupe, filter `rows` before calling (this is a plain INSERT)."""
    if not rows:
        return 0
    for i in range(0, len(rows), batch_size):
        db.execute(insert(model), rows[i:i + batch_size])
    db.commit()
    return len(rows)
