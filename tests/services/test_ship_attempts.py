"""Tests for ShipAttemptsStore."""

import json

import pytest

from app.models.database import init_all_dbs
from app.services.ship_attempts import (
    ShipAttemptsStore,
    _body_sha256,
    _canonical_body,
    new_idempotency_key,
)


@pytest.fixture
def store(app):
    """Ship attempts share the same SQLite file as ship_history;
    the session-scoped `app` fixture redirects both to a temp dir."""
    init_all_dbs()
    return ShipAttemptsStore()


class TestCanonicalBody:

    def test_key_order_is_stable(self):
        a = _canonical_body({'b': 2, 'a': 1, 'c': 3})
        b = _canonical_body({'a': 1, 'c': 3, 'b': 2})
        assert a == b

    def test_hash_changes_with_value(self):
        h1 = _body_sha256({'tracking': '1Z'})
        h2 = _body_sha256({'tracking': '2Z'})
        assert h1 != h2

    def test_hash_stable_under_reorder(self):
        h1 = _body_sha256({'a': 1, 'b': 2})
        h2 = _body_sha256({'b': 2, 'a': 1})
        assert h1 == h2


class TestStoreLifecycle:

    def test_insert_pending_then_success(self, store):
        key = new_idempotency_key()
        store.insert_pending(
            idempotency_key=key, operation='ship', so_number='SO-1',
            request_body={'tracking': '1Z', 'carrier': 'UPS'},
        )
        row = store.get(key)
        assert row['status'] == 'pending'
        assert row['operation'] == 'ship'
        assert row['so_number'] == 'SO-1'
        assert row['attempt_count'] == 1
        assert json.loads(row['request_body']) == {'carrier': 'UPS', 'tracking': '1Z'}

        store.mark_success(key, response_body={'fulfillment_id': 42})
        row = store.get(key)
        assert row['status'] == 'success'
        assert row['attempt_count'] == 2
        assert json.loads(row['response_body'])['fulfillment_id'] == 42

    def test_insert_then_unknown(self, store):
        key = new_idempotency_key()
        store.insert_pending(
            idempotency_key=key, operation='ship', so_number='SO-2',
            request_body={'tracking': 'T'},
        )
        store.mark_unknown(key, error_kind='timeout', message='read timeout')
        row = store.get(key)
        assert row['status'] == 'unknown'
        assert row['error_kind'] == 'timeout'

    def test_insert_then_rejected(self, store):
        key = new_idempotency_key()
        store.insert_pending(
            idempotency_key=key, operation='ship', so_number='SO-3',
            request_body={'tracking': 'T'},
        )
        store.mark_rejected(
            key,
            error_kind='already_shipped',
            message='shipped',
            details={'existing_tracking': '1Z'},
            response_status=409,
        )
        row = store.get(key)
        assert row['status'] == 'rejected'
        assert row['error_kind'] == 'already_shipped'
        assert row['response_status'] == 409
        assert json.loads(row['response_body'])['details']['existing_tracking'] == '1Z'

    def test_rejects_invalid_operation(self, store):
        with pytest.raises(ValueError):
            store.insert_pending(
                idempotency_key=new_idempotency_key(),
                operation='oops',
                so_number='SO-9',
                request_body={},
            )

    def test_duplicate_key_raises(self, store):
        import sqlite3
        key = new_idempotency_key()
        store.insert_pending(
            idempotency_key=key, operation='ship', so_number='SO-4',
            request_body={},
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_pending(
                idempotency_key=key, operation='ship', so_number='SO-4',
                request_body={},
            )


class TestRecoverable:

    def test_find_recoverable_returns_pending_and_unknown(self, store):
        k1 = new_idempotency_key()
        k2 = new_idempotency_key()
        k3 = new_idempotency_key()
        k4 = new_idempotency_key()
        store.insert_pending(idempotency_key=k1, operation='ship', so_number='SO-A', request_body={})
        store.insert_pending(idempotency_key=k2, operation='ship', so_number='SO-B', request_body={})
        store.mark_unknown(k2, error_kind='timeout')
        store.insert_pending(idempotency_key=k3, operation='ship', so_number='SO-C', request_body={})
        store.mark_success(k3)
        store.insert_pending(idempotency_key=k4, operation='ship', so_number='SO-D', request_body={})
        store.mark_rejected(k4, error_kind='already_shipped', message='x')

        rows = store.find_recoverable()
        keys = {r['idempotency_key'] for r in rows}
        assert k1 in keys  # pending
        assert k2 in keys  # unknown
        assert k3 not in keys  # success
        assert k4 not in keys  # rejected


class TestPrune:

    def test_prune_terminal_keeps_recent(self, store):
        k = new_idempotency_key()
        store.insert_pending(idempotency_key=k, operation='ship', so_number='SO-P', request_body={})
        store.mark_success(k)
        deleted = store.prune_terminal(older_than_days=14)
        assert deleted == 0
        assert store.get(k) is not None

    def test_prune_terminal_keeps_unknown(self, store):
        k = new_idempotency_key()
        store.insert_pending(idempotency_key=k, operation='ship', so_number='SO-Q', request_body={})
        store.mark_unknown(k, error_kind='timeout')
        # Even with older_than_days=0, an unknown row is never pruned.
        store.prune_terminal(older_than_days=0)
        assert store.get(k) is not None
