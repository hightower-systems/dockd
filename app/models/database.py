"""Postgres connection layer for Dockd.

Dockd's three operational tables all live in one Postgres database:

- ``ship_history``  -- order tracking, carrier costs, timing metrics (insert-only)
- ``ship_attempts`` -- crash-recovery idempotency journal (read back on boot)
- ``override_log``  -- audit trail of manual overrides (insert-only)

Schema is owned by Alembic (``alembic upgrade head`` at boot), not by this
module. This module owns only the connection pool and the acquisition seam
the services use.

The move off SQLite-on-SMB (Azure Files) retires the ``nolock=1`` /
``journal_mode`` workarounds that could not make two concurrent writers safe
during a Container Apps revision swap -- the corruption vector behind the
recurring ``database disk image is malformed`` incidents.
"""

import logging
import os
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger('dockd.database')


_pool = None
_pool_lock = threading.Lock()


def _database_url() -> str:
    url = (os.environ.get('DATABASE_URL') or '').strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set; Dockd requires a Postgres connection. "
            "Set it to a libpq DSN, e.g. postgresql://dockd_app:...@host:5432/dockd"
        )
    return url


def init_pool(minconn: int = 1, maxconn: int = None):
    """Create the process-wide connection pool (idempotent).

    Flask runs ``threaded=True`` and every service opens a connection per
    operation, so a ``ThreadedConnectionPool`` is the right shape: it hands
    the same small set of connections back and forth across worker threads.
    Called once from the app factory; safe to call again (no-op after the
    first).
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            if maxconn is None:
                maxconn = int(os.environ.get('DOCKD_DB_POOL_MAX', '5'))
            _pool = ThreadedConnectionPool(
                minconn, maxconn, dsn=_database_url(),
                cursor_factory=RealDictCursor,
            )
            logger.info(
                "Postgres pool initialized (min=%d max=%d)", minconn, maxconn,
            )
    return _pool


def close_pool():
    """Dispose of the pool (used by tests / clean shutdown)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


def _get_pool():
    if _pool is None:
        init_pool()
    return _pool


@contextmanager
def get_db():
    """Yield a pooled connection for one unit of work.

    Usage mirrors the old ``get_ship_db()`` handles but as a context
    manager, so callers never leak a connection back to the pool in a
    dirty state::

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT ... VALUES (%s)", (x,))
            conn.commit()

    On any exception the transaction is rolled back before the connection
    returns to the pool -- critical after a ``UniqueViolation`` (which
    ``ship_attempts.insert_pending`` relies on raising), because an aborted
    transaction must be cleared before the connection is reused.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# Backward-compatible names. All three SQLite files collapsed into one
# Postgres database, so both handles resolve to the same pool; the split
# names are kept only for call-site readability.
get_ship_db = get_db
get_override_db = get_db


__all__ = [
    'init_pool',
    'close_pool',
    'get_db',
    'get_ship_db',
    'get_override_db',
    'RealDictCursor',
    'psycopg2',
]
