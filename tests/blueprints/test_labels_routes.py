"""Blueprint tests for /bins/print, /labels/catalog, /labels/print.

Drives the routes through Flask's test client with a mocked backend.
The label_cache, printer, and shiprush services are not touched by
these routes -- the browser-side scale agent is responsible for the
actual ZPL delivery -- so only the backend lookup is mocked.
"""

import base64
from unittest.mock import MagicMock

import pytest

from app.services.backend import ItemData, NetworkError, NotFoundError


# ----- /bins/print ---------------------------------------------------------


def _decode_zpl(payload):
    return base64.b64decode(payload['zpl_b64']).decode('utf-8')


class TestBinsPrint:

    def test_happy_path_returns_zpl_and_metadata(self, auth_client, mock_backend):
        mock_backend.lookup_item.return_value = ItemData(
            item_id=42, sku='1264-42316', item_name='Antron Yarn Black',
            upc='053526423167', quantity_on_hand=37,
        )
        resp = auth_client.post('/bins/print', json={'scan_data': '053526423167'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['status'] == 'success'
        assert body['sku'] == '1264-42316'
        assert body['upc'] == '053526423167'
        assert body['quantity'] == 37
        mock_backend.lookup_item.assert_called_once_with('053526423167')
        zpl = _decode_zpl(body)
        assert '^PW812' in zpl  # 4x2 sticker
        assert '1264-42316' in zpl
        assert '053526423167' in zpl

    def test_returns_404_when_item_missing(self, auth_client, mock_backend):
        mock_backend.lookup_item.side_effect = NotFoundError(
            error_kind='not_found', status_code=404,
        )
        resp = auth_client.post('/bins/print', json={'scan_data': 'NOPE-999'})
        assert resp.status_code == 404
        assert resp.get_json()['status'] == 'error'

    def test_returns_502_on_network_error(self, auth_client, mock_backend):
        mock_backend.lookup_item.side_effect = NetworkError(
            error_kind='timeout', message='timeout',
        )
        resp = auth_client.post('/bins/print', json={'scan_data': '053526423167'})
        assert resp.status_code == 502

    def test_rejects_empty_scan(self, auth_client, mock_backend):
        resp = auth_client.post('/bins/print', json={'scan_data': '   '})
        assert resp.status_code == 400
        mock_backend.lookup_item.assert_not_called()

    def test_rejects_obviously_unsafe_scan(self, auth_client, mock_backend):
        resp = auth_client.post('/bins/print', json={'scan_data': "x'; DROP--"})
        assert resp.status_code == 400
        mock_backend.lookup_item.assert_not_called()

    def test_requires_login(self, client):
        resp = client.post('/bins/print', json={'scan_data': '053526423167'})
        assert resp.status_code == 401

    def test_returns_503_when_backend_unconfigured(self, auth_client, app):
        original = app.shipping_service.backend
        app.shipping_service.backend = None
        try:
            resp = auth_client.post('/bins/print', json={'scan_data': '053526423167'})
            assert resp.status_code == 503
        finally:
            app.shipping_service.backend = original

    def test_falls_back_to_sku_only_zpl_when_no_upc(self, auth_client, mock_backend):
        mock_backend.lookup_item.return_value = ItemData(
            item_id=42, sku='WIDGET-A', item_name='Widget',
            upc=None, quantity_on_hand=0,
        )
        resp = auth_client.post('/bins/print', json={'scan_data': 'WIDGET-A'})
        assert resp.status_code == 200
        zpl = _decode_zpl(resp.get_json())
        assert 'WIDGET-A' in zpl
        assert '^BCN' not in zpl  # no barcode block


# ----- /labels/catalog -----------------------------------------------------


class TestLabelsCatalog:

    def test_returns_catalog_when_loaded(self, auth_client, app):
        original = app.thread_catalog
        fake = MagicMock()
        fake.is_empty.return_value = False
        fake.as_response.return_value = {
            'brands': ['UTC'],
            'catalog': {'UTC': {'sizes': ['70 Denier'], 'products': {}}},
        }
        app.thread_catalog = fake
        try:
            resp = auth_client.get('/labels/catalog')
            assert resp.status_code == 200
            assert resp.get_json()['brands'] == ['UTC']
        finally:
            app.thread_catalog = original

    def test_returns_404_when_empty(self, auth_client, app):
        original = app.thread_catalog
        fake = MagicMock()
        fake.is_empty.return_value = True
        app.thread_catalog = fake
        try:
            resp = auth_client.get('/labels/catalog')
            assert resp.status_code == 404
        finally:
            app.thread_catalog = original


# ----- /labels/print -------------------------------------------------------


class TestLabelsPrint:

    def test_thread_catalog_flow_skips_backend_lookup(self, auth_client, mock_backend):
        resp = auth_client.post('/labels/print', json={
            'upc': '053526423167',
            'sku': '1264-42316',
            'quantity': 3,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['quantity'] == 3
        assert body['upc'] == '053526423167'
        mock_backend.lookup_item.assert_not_called()
        zpl = _decode_zpl(body)
        # Three copies of the 1.5x1 label concatenated.
        assert zpl.count('^XA') == 3
        assert zpl.count('053526423167') == 3
        assert '^PW304' in zpl

    def test_item_lookup_flow_resolves_sku_to_upc(self, auth_client, mock_backend):
        mock_backend.lookup_item.return_value = ItemData(
            item_id=42, sku='1264-42316', item_name='Antron Yarn',
            upc='053526423167', quantity_on_hand=12,
        )
        resp = auth_client.post('/labels/print', json={
            'sku': '1264-42316',
            'quantity': 2,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['upc'] == '053526423167'
        assert body['sku'] == '1264-42316'
        assert body['quantity'] == 2
        mock_backend.lookup_item.assert_called_once_with('1264-42316')

    def test_clamps_quantity_high_and_low(self, auth_client):
        resp_hi = auth_client.post('/labels/print', json={'upc': '012345678905', 'quantity': 999})
        assert resp_hi.get_json()['quantity'] == 100
        resp_lo = auth_client.post('/labels/print', json={'upc': '012345678905', 'quantity': 0})
        assert resp_lo.get_json()['quantity'] == 1

    def test_rejects_when_neither_upc_nor_sku_provided(self, auth_client):
        resp = auth_client.post('/labels/print', json={'quantity': 5})
        assert resp.status_code == 400

    def test_returns_422_when_item_has_no_upc(self, auth_client, mock_backend):
        mock_backend.lookup_item.return_value = ItemData(
            item_id=42, sku='WIDGET-A', item_name='Widget',
            upc=None, quantity_on_hand=3,
        )
        resp = auth_client.post('/labels/print', json={'sku': 'WIDGET-A', 'quantity': 1})
        assert resp.status_code == 422

    def test_returns_404_when_sku_unknown(self, auth_client, mock_backend):
        mock_backend.lookup_item.side_effect = NotFoundError(
            error_kind='not_found', status_code=404,
        )
        resp = auth_client.post('/labels/print', json={'sku': 'NOPE', 'quantity': 1})
        assert resp.status_code == 404

    def test_rejects_non_alnum_upc_input(self, auth_client):
        resp = auth_client.post('/labels/print', json={'upc': "'); DROP--", 'quantity': 1})
        assert resp.status_code == 400

    def test_requires_login(self, client):
        resp = client.post('/labels/print', json={'upc': '012345678905', 'quantity': 1})
        assert resp.status_code == 401
