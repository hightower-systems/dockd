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

# TCP keepalives so the OS reaps and re-establishes a connection the server
# (or the Azure Files / Container Apps ingress path) silently dropped while
# idle, instead of handing a caller a dead socket. connect_timeout bounds a
# hung connect. These are libpq connection keywords, applied to every pooled
# connection.
_KEEPALIVE_KWARGS = {
    'keepalives': 1,
    'keepalives_idle': 30,
    'keepalives_interval': 10,
    'keepalives_count': 3,
    'connect_timeout': 10,
}

# Fallback pool ceiling for the lazy _get_pool() path only. The real entry
# (the app factory) passes config.DB_POOL_MAX, which is the single reader of
# the DOCKD_DB_POOL_MAX env var; this constant just keeps a no-arg init_pool()
# safe.
_DEFAULT_POOL_MAX = 5

# How many pooled connections get_db() will cycle through looking for a live
# one before giving up. A dropped-but-not-yet-detected connection is discarded
# and the next is tried; a fresh connect (the pool grows to maxconn on demand)
# ends the loop. Only every connection being dead -- i.e. the server is truly
# unreachable -- exhausts this, which is the loud failure we want.
_MAX_CHECKOUT_TRIES = 3


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
    Called once from the app factory with ``config.DB_POOL_MAX`` (the single
    reader of the ``DOCKD_DB_POOL_MAX`` env var); safe to call again (no-op
    after the first). The lazy fallback below only runs if a caller reaches
    the pool before the factory sized it.
    """
    global _pool
    with _pool_lock:
        if _pool is None:
            if maxconn is None:
                maxconn = _DEFAULT_POOL_MAX
            _pool = ThreadedConnectionPool(
                minconn, maxconn, dsn=_database_url(),
                cursor_factory=RealDictCursor,
                **_KEEPALIVE_KWARGS,
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


def _is_live(conn) -> bool:
    """True if the connection answers a trivial round-trip.

    A connection the server dropped while idle looks fine until the first
    query, which then fails. Probing on checkout lets get_db() discard the
    dead one and hand back a working connection, so the caller's real work
    (e.g. the post-label ship_history / override_log INSERT, whose failure
    is swallowed by its caller) never lands on a corpse socket.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        # No rollback here: the caller's work joins this transaction and
        # commits/rolls back as its own unit (the same way read-only callers
        # already leave their SELECT's transaction to the caller). Rolling
        # back on checkout would also clobber the shared-transaction test
        # harness.
        return True
    except Exception:
        return False


@contextmanager
def get_db():
    """Yield a live pooled connection for one unit of work.

    Usage mirrors the old ``get_ship_db()`` handles but as a context
    manager, so callers never leak a connection back to the pool in a
    dirty state::

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT ... VALUES (%s)", (x,))
            conn.commit()

    The yielded connection is probed live on checkout (see ``_is_live``);
    any the server dropped while idle are closed and skipped. On any
    exception the transaction is rolled back before the connection returns
    to the pool -- critical after a ``UniqueViolation`` (which
    ``ship_attempts.insert_pending`` relies on raising), because an aborted
    transaction must be cleared before the connection is reused.
    """
    pool = _get_pool()
    conn = None
    for _ in range(_MAX_CHECKOUT_TRIES):
        candidate = pool.getconn()
        if _is_live(candidate):
            conn = candidate
            break
        # Dead socket: drop it from the pool entirely (close=True) so it is
        # never handed out again, then try for another / a fresh one.
        pool.putconn(candidate, close=True)
    if conn is None:
        raise psycopg2.OperationalError(
            f"no live Postgres connection after {_MAX_CHECKOUT_TRIES} attempts"
        )

    broken = False
    try:
        yield conn
    except Exception:
        # Clear the aborted transaction before reuse. Guard it: if the
        # connection itself died mid-operation, rollback() raises, and an
        # unguarded rollback here would REPLACE the caller's real exception
        # with a useless InterfaceError -- masking the actual failure. Swallow
        # the rollback error and mark the connection broken so it is discarded.
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        pool.putconn(conn, close=bool(broken or conn.closed))


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
