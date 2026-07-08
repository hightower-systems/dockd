"""Unit tests for the get_db() checkout contract.

These exercise the two durability guarantees added for #12 without a real
Postgres, by swapping the module pool for a fake:

  1. A connection the server dropped while idle is detected on checkout and
     skipped, so callers (notably the post-label ship_history / override_log
     INSERTs, whose failures are swallowed) never land on a dead socket.
  2. When a caller's block raises AND the connection's own rollback then
     raises (a dead connection), the caller's original exception propagates -
     the rollback failure must not mask it - and the connection is discarded.
"""

import psycopg2
import pytest

from app.models import database


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if not self._conn.alive:
            raise psycopg2.OperationalError("server closed the connection")

    def fetchone(self):
        return {"?column?": 1}


class _FakeConn:
    def __init__(self, alive=True, rollback_raises=False):
        self.alive = alive
        self.closed = 0
        self.rollback_raises = rollback_raises
        self.rolled_back = False

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def rollback(self):
        self.rolled_back = True
        if self.rollback_raises:
            raise psycopg2.InterfaceError("connection already closed")


class _FakePool:
    def __init__(self, conns):
        self._conns = list(conns)
        self.returned = []  # (conn, close) in putconn order

    def getconn(self):
        return self._conns.pop(0)

    def putconn(self, conn, close=False):
        self.returned.append((conn, close))


@pytest.fixture
def swap_pool(monkeypatch):
    def _swap(pool):
        monkeypatch.setattr(database, "_pool", pool)
        return pool
    return _swap


def test_dead_connection_is_skipped_and_discarded(swap_pool):
    dead = _FakeConn(alive=False)
    live = _FakeConn(alive=True)
    pool = swap_pool(_FakePool([dead, live]))

    with database.get_db() as conn:
        assert conn is live

    # The dead one was force-closed out of the pool; the live one returned open.
    assert (dead, True) in pool.returned
    assert (live, False) in pool.returned


def test_all_connections_dead_raises(swap_pool):
    deads = [_FakeConn(alive=False) for _ in range(database._MAX_CHECKOUT_TRIES)]
    swap_pool(_FakePool(deads))

    with pytest.raises(psycopg2.OperationalError):
        with database.get_db():
            pass


def test_rollback_failure_does_not_mask_original_error(swap_pool):
    # The connection pings alive, but dies mid-work: the caller raises, and the
    # rollback in the except-path itself raises. The caller's exception must win.
    conn = _FakeConn(alive=True, rollback_raises=True)
    pool = swap_pool(_FakePool([conn]))

    with pytest.raises(ValueError, match="real failure"):
        with database.get_db():
            raise ValueError("real failure")

    assert conn.rolled_back is True
    # A connection whose rollback failed is broken -> discarded, not recycled.
    assert (conn, True) in pool.returned


def test_clean_error_rolls_back_and_recycles(swap_pool):
    # A normal in-block error on a healthy connection: rollback succeeds, the
    # connection is returned to the pool (not force-closed).
    conn = _FakeConn(alive=True, rollback_raises=False)
    pool = swap_pool(_FakePool([conn]))

    with pytest.raises(KeyError):
        with database.get_db():
            raise KeyError("boom")

    assert conn.rolled_back is True
    assert (conn, False) in pool.returned
