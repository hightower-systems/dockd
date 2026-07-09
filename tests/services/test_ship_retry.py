"""Integration tests for ShippingService.retry_recoverable_attempts.

Exercises the boot-retry path that drains pending / unknown
ship_attempts rows on dockd restart. The backend is mocked; the
ship_attempts table is real (sits inside the per-test temp DB).
"""

from unittest.mock import MagicMock

import pytest

from app.services.backend import (
    AlreadyShippedError,
    NetworkError,
    ShipResult,
    VoidResult,
)
from app.services.ship_attempts import new_idempotency_key

# No table-clearing fixture is needed: conftest rolls back each test's
# transaction, so ship_attempts starts empty for every test and
# find_recoverable() returns only the rows a test inserts itself.


class TestRetryRecoverable:

    def test_retry_pending_ship_marks_success(self, app):
        svc = app.shipping_service
        backend = MagicMock()
        backend.confirm_shipped.return_value = ShipResult(
            status='SHIPPED', tracking='1Z',
            shipped_at='2026-05-11T00:00:00Z',
            fulfillment_id=1, audit_log_id=2,
        )
        svc.backend = backend
        try:
            key = new_idempotency_key()
            svc.ship_attempts.insert_pending(
                idempotency_key=key, operation='ship', so_number='SO-1',
                request_body={
                    'tracking': '1Z', 'carrier': 'UPS', 'ship_method': None,
                    'operator_username': 'TestUser', 'shipping_cost': None,
                    'weight': None, 'dims': None, 'manual_link': False,
                    'idempotency_key': key,
                },
            )

            results = svc.retry_recoverable_attempts()
            assert (key, 'success') in results
            row = svc.ship_attempts.get(key)
            assert row['status'] == 'success'
            backend.confirm_shipped.assert_called_once()
            assert backend.confirm_shipped.call_args.kwargs['idempotency_key'] == key
        finally:
            svc.backend = None

    def test_retry_unknown_stays_unknown_on_network_error(self, app):
        svc = app.shipping_service
        backend = MagicMock()
        backend.confirm_shipped.side_effect = NetworkError(
            error_kind='timeout', message='read timeout',
        )
        svc.backend = backend
        try:
            key = new_idempotency_key()
            svc.ship_attempts.insert_pending(
                idempotency_key=key, operation='ship', so_number='SO-2',
                request_body={
                    'tracking': '1Z', 'carrier': 'UPS', 'ship_method': None,
                    'operator_username': 'TestUser', 'shipping_cost': None,
                    'weight': None, 'dims': None, 'manual_link': False,
                    'idempotency_key': key,
                },
            )
            svc.ship_attempts.mark_unknown(key, error_kind='timeout')

            results = svc.retry_recoverable_attempts()
            assert (key, 'unknown') in results
            row = svc.ship_attempts.get(key)
            assert row['status'] == 'unknown'
            # attempt_count: 1 (insert) + 1 (mark_unknown) + 1 (retry).
            assert row['attempt_count'] >= 3
        finally:
            svc.backend = None

    def test_retry_terminal_4xx_marks_rejected(self, app):
        svc = app.shipping_service
        backend = MagicMock()
        backend.confirm_shipped.side_effect = AlreadyShippedError(
            error_kind='already_shipped',
            message='shipped',
            details={'existing_tracking': '1Z'},
            status_code=409,
        )
        svc.backend = backend
        try:
            key = new_idempotency_key()
            svc.ship_attempts.insert_pending(
                idempotency_key=key, operation='ship', so_number='SO-3',
                request_body={
                    'tracking': '1Z', 'carrier': 'UPS', 'ship_method': None,
                    'operator_username': 'TestUser', 'shipping_cost': None,
                    'weight': None, 'dims': None, 'manual_link': False,
                    'idempotency_key': key,
                },
            )

            results = svc.retry_recoverable_attempts()
            assert (key, 'rejected') in results
            row = svc.ship_attempts.get(key)
            assert row['status'] == 'rejected'
            assert row['error_kind'] == 'already_shipped'
        finally:
            svc.backend = None

    def test_retry_void_dispatches_correctly(self, app):
        svc = app.shipping_service
        backend = MagicMock()
        backend.void_ship.return_value = VoidResult(
            status='PACKED', voided_at='2026-05-11T00:00:00Z', audit_log_id=10,
        )
        svc.backend = backend
        try:
            key = new_idempotency_key()
            svc.ship_attempts.insert_pending(
                idempotency_key=key, operation='void', so_number='SO-4',
                request_body={
                    'reason': 'box damaged', 'operator_username': 'TestUser',
                    'idempotency_key': key,
                },
            )

            results = svc.retry_recoverable_attempts()
            assert (key, 'success') in results
            backend.void_ship.assert_called_once()
            assert backend.void_ship.call_args.kwargs['reason'] == 'box damaged'
            assert backend.void_ship.call_args.kwargs['idempotency_key'] == key
        finally:
            svc.backend = None

    def test_retry_skips_when_no_backend(self, app):
        svc = app.shipping_service
        # No backend, no retry; safe to call (used in tests where the
        # boot retry flag is set but no backend is wired).
        svc.backend = None
        results = svc.retry_recoverable_attempts()
        assert results == []
