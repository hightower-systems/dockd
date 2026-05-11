"""Tests for shipping blueprint routes."""

import pytest
from unittest.mock import patch


class TestShipOrderRoutes:

    def test_index_page(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'DOCKD' in resp.data

    def test_get_order_details_requires_auth(self, client):
        resp = client.get('/get_order_details?ticket=SO123')
        # GET returns JS redirect for browser, not 401
        assert resp.status_code == 200
        assert b'window.location' in resp.data

    def test_ship_order_requires_auth(self, client):
        resp = client.post('/ship_order', json={})
        assert resp.status_code == 401

    def test_reprint_requires_auth(self, client):
        resp = client.post('/reprint_label', json={'ticket': '12345'})
        assert resp.status_code == 401

    def test_void_requires_auth(self, client):
        resp = client.post('/void_label', json={'ticket': '12345'})
        assert resp.status_code == 401

    def test_ship_count(self, auth_client):
        resp = auth_client.get('/ship_count')
        data = resp.get_json()
        assert 'count' in data

    def test_manual_link_requires_auth(self, client):
        resp = client.post('/manual_link_tracking', json={})
        assert resp.status_code == 401

    def test_log_override(self, auth_client):
        resp = auth_client.post('/log_override', json={
            'order_number': 'SO123',
            'user': 'TestUser',
            'items': [{'item_name': 'Test Item', 'sku': 'TST-001'}],
            'override_type': 'single',
        })
        data = resp.get_json()
        assert data['status'] == 'ok'

    def test_ship_order_without_backend_returns_error(self, auth_client):
        """With no order backend wired (Sentry integration pending),
        the route returns a structured error instead of crashing."""
        resp = auth_client.post('/ship_order', json={
            'fulfillment_id': '12345',
            'box_id': '1',
            'weight': 1.5,
            'order_number': 'SO999',
        })
        data = resp.get_json()
        assert data['status'] == 'error'
        assert 'backend' in data['message'].lower()
