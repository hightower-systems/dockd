"""Tests for BackendHealth."""

import time
from unittest.mock import MagicMock

from app.services.backend_health import BackendHealth


class TestStates:

    def test_not_configured_when_backend_none(self):
        h = BackendHealth(None)
        s = h.status()
        assert s['state'] == 'not_configured'
        assert s['consecutive_failures'] == 0

    def test_first_probe_ok(self):
        backend = MagicMock()
        backend.health.return_value = True
        h = BackendHealth(backend, cache_ttl_seconds=10)
        s = h.status()
        assert s['state'] == 'ok'
        assert s['consecutive_failures'] == 0
        assert s['last_success_seconds_ago'] is not None
        backend.health.assert_called_once()

    def test_cached_no_second_call(self):
        backend = MagicMock()
        backend.health.return_value = True
        h = BackendHealth(backend, cache_ttl_seconds=10)
        h.status()
        h.status()
        h.status()
        # Cache TTL not yet elapsed; only the first call hit the backend.
        backend.health.assert_called_once()

    def test_degraded_after_one_failure(self):
        backend = MagicMock()
        backend.health.return_value = False
        h = BackendHealth(backend, cache_ttl_seconds=0, failure_threshold=3)
        s = h.status()
        assert s['state'] == 'degraded'
        assert s['consecutive_failures'] == 1

    def test_down_after_threshold(self):
        backend = MagicMock()
        backend.health.return_value = False
        h = BackendHealth(backend, cache_ttl_seconds=0, failure_threshold=3)
        h.status()
        h.status()
        h.status()
        s = h.status()
        assert s['state'] == 'down'
        assert s['consecutive_failures'] >= 3

    def test_recovery_resets_failure_count(self):
        backend = MagicMock()
        # Two failures, then a success.
        backend.health.side_effect = [False, False, True]
        h = BackendHealth(backend, cache_ttl_seconds=0, failure_threshold=3)
        h.status()
        h.status()
        s = h.status()
        assert s['state'] == 'ok'
        assert s['consecutive_failures'] == 0

    def test_exception_counts_as_failure(self):
        backend = MagicMock()
        backend.health.side_effect = RuntimeError("boom")
        h = BackendHealth(backend, cache_ttl_seconds=0, failure_threshold=3)
        s = h.status()
        assert s['state'] == 'degraded'
        assert 'RuntimeError' in (s['last_error'] or '')


class TestRouteIntegration:

    def test_health_endpoint_returns_state(self, auth_client, app):
        # Default backend is None in test app; expect not_configured.
        resp = auth_client.get('/api/health/backend')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'state' in body
        assert body['state'] == 'not_configured'

    def test_health_endpoint_requires_auth(self, client):
        resp = client.get('/api/health/backend')
        assert resp.status_code == 401
