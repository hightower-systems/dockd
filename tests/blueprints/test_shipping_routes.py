"""Tests for shipping blueprint routes."""

from app.services.backend import (
    AlreadyShippedError,
    NotFoundError,
    OrderData,
    OrderItem,
    ShippingAddress,
    ShipResult,
    VoidResult,
)


def _sample_order(so_number="SO-1001", status="PICKED", shipped=False):
    return OrderData(
        so_number=so_number,
        external_id="00000000-0000-0000-0000-000000000001",
        status=status,
        warehouse_id=7,
        shippable=(not shipped),
        shippable_from_statuses=["PICKED", "PACKED"],
        items=[
            OrderItem(
                external_id="00000000-0000-0000-0000-000000000099",
                sku="WIDGET-A",
                display_name="Blue Widget",
                upc="012345678905",
                qty=2,
            ),
        ],
        shipping_address=ShippingAddress(
            name="Pat Q. Customer",
            line1="123 Main St",
            line2=None,
            city="Denver",
            state="CO",
            postal_code="80202",
            country="US",
            phone="555-0100",
        ),
        customer_name="Pat Q. Customer",
        customer_phone="555-0100",
        ship_method="USPS Ground Advantage",
        memo=None,
        order_total=45.67,
        customer_shipping_paid=3.50,
        marketplace="amazon",
        order_date="2026-05-10T12:00:00+00:00",
        ff_created_at="2026-05-10T12:34:56+00:00",
    )


class TestShipOrderRoutes:

    def test_index_page(self, client):
        resp = client.get('/')
        assert resp.status_code == 200
        assert b'DOCKD' in resp.data

    def test_get_order_details_requires_auth(self, client):
        resp = client.get('/get_order_details?so_number=SO123')
        # GET returns JS redirect for browser, not 401
        assert resp.status_code == 200
        assert b'window.location' in resp.data

    def test_ship_order_requires_auth(self, client):
        resp = client.post('/ship_order', json={})
        assert resp.status_code == 401

    def test_reprint_requires_auth(self, client):
        resp = client.post('/reprint_label', json={'so_number': '12345'})
        assert resp.status_code == 401

    def test_void_requires_auth(self, client):
        resp = client.post('/void_label', json={'so_number': '12345'})
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
        """With no order backend wired the route surfaces a structured
        "backend not configured" error instead of crashing."""
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO999',
            'box_id': '1',
            'weight': 1.5,
        })
        data = resp.get_json()
        assert data['status'] == 'error'
        assert 'backend' in data['message'].lower()


class TestLoadOrderWithBackend:

    def test_load_order_success(self, auth_client, mock_backend):
        mock_backend.get_order.return_value = _sample_order(so_number="SO-1001")
        resp = auth_client.get('/get_order_details?so_number=SO-1001')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['so_number'] == 'SO-1001'
        assert data['order_number'] == 'SO-1001'
        assert data['ship_method'] == 'USPS Ground Advantage'
        assert data['address']['name'] == 'Pat Q. Customer'
        assert data['address']['addr1'] == '123 Main St'
        assert data['address']['zip'] == '80202'
        assert data['order_total'] == 45.67
        assert data['ca_shipping_paid'] == 3.50
        assert data['amazon_order_id'] == ''  # deprecated, kept for back-compat
        assert data['marketplace'] == 'amazon'
        assert len(data['items']) == 1
        assert data['items'][0]['sku'] == 'WIDGET-A'
        assert data['items'][0]['qty'] == 2
        mock_backend.get_order.assert_called_once_with('SO-1001')

    def test_load_order_not_found(self, auth_client, mock_backend):
        mock_backend.get_order.side_effect = NotFoundError(
            error_kind='not_found', message='order not found', status_code=404,
        )
        resp = auth_client.get('/get_order_details?so_number=SO-MISSING')
        data = resp.get_json()
        assert data['status'] == 'error'
        assert 'not found' in data['message'].lower()

    def test_load_order_accepts_legacy_ticket_param(self, auth_client, mock_backend):
        """Stale browser sessions may still send `ticket`; the route
        accepts it as a fallback for `so_number`."""
        mock_backend.get_order.return_value = _sample_order(so_number='SO-LEGACY')
        resp = auth_client.get('/get_order_details?ticket=SO-LEGACY')
        assert resp.status_code == 200
        assert resp.get_json()['so_number'] == 'SO-LEGACY'


class TestShipOrderWithBackend:

    def test_ship_order_happy_path(self, auth_client, mock_backend, mock_shiprush):
        mock_backend.get_order.return_value = _sample_order(so_number='SO-7')
        mock_backend.confirm_shipped.return_value = ShipResult(
            status='SHIPPED',
            tracking='1Z999AA10123456784',
            shipped_at='2026-05-11T15:30:00Z',
            fulfillment_id=42,
            audit_log_id=9001,
        )
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO-7',
            'box_id': '1',
            'weight': 1.5,
            'ca_shipping_paid': 3.50,
            'station_id': 'pack-station-1',
            'station_label': 'Pack Station 1',
        })
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['tracking'] == '1Z999AA10123456784'
        assert data['sentry_fulfillment_id'] == 42
        assert data['sentry_audit_log_id'] == 9001
        # Print flow flip (v0.3.0): zpl_b64 is returned for the
        # browser to forward; the server no longer calls a printer.
        assert data['zpl_b64'] == 'XlhBClRFU1QKXlha'

        # Backend was called for both load + confirm.
        mock_backend.get_order.assert_called_once_with('SO-7')
        assert mock_backend.confirm_shipped.called
        call = mock_backend.confirm_shipped.call_args
        assert call.kwargs['tracking'] == '1Z999AA10123456784'
        # stikman28/dockd#6: the order's ship method is "USPS Ground
        # Advantage" but ShipRush returned a 1Z (UPS) label, so the
        # carrier recorded upstream must follow the tracking number
        # (UPS), not the requested method (which would say USPS).
        assert call.kwargs['carrier'] == 'UPS'
        assert call.kwargs['operator_username'] == 'TestUser'
        assert call.kwargs['manual_link'] is False
        # idempotency_key is a UUID4 string generated per call.
        assert isinstance(call.kwargs['idempotency_key'], str)
        assert len(call.kwargs['idempotency_key']) == 36

    def test_ship_order_reports_usps_carrier_from_9_tracking(
            self, auth_client, mock_backend, mock_shiprush):
        """stikman28/dockd#6, the other direction: when ShipRush returns
        a USPS (9...) label the carrier recorded upstream is USPS,
        derived from the tracking prefix rather than re-parsed from the
        ship method."""
        mock_backend.get_order.return_value = _sample_order(so_number='SO-USPS')
        mock_shiprush.generate_label.return_value = {
            'status': 'success',
            'tracking': '9405511899223456781234',
            'zpl_b64': 'XlhBClRFU1QKXlha',
            'cost': 6.10,
        }
        mock_backend.confirm_shipped.return_value = ShipResult(
            status='SHIPPED', tracking='9405511899223456781234',
            shipped_at='2026-05-11T15:30:00Z',
            fulfillment_id=45, audit_log_id=9004,
        )
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO-USPS',
            'box_id': '1',
            'weight': 1.0,
        })
        assert resp.get_json()['status'] == 'success'
        assert mock_backend.confirm_shipped.call_args.kwargs['carrier'] == 'USPS'

    def test_ship_order_already_shipped(self, auth_client, mock_backend,
                                        mock_shiprush):
        mock_backend.get_order.return_value = _sample_order(so_number='SO-8')
        mock_backend.confirm_shipped.side_effect = AlreadyShippedError(
            error_kind='already_shipped',
            message='shipped',
            details={'existing_tracking': '1Z999AA10123456784'},
            status_code=409,
        )
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO-8',
            'box_id': '1',
            'weight': 1.5,
        })
        data = resp.get_json()
        assert data['status'] == 'error'
        assert '1Z999AA10123456784' in data['message']

    def test_ship_order_invalid_so_number(self, auth_client, mock_backend):
        resp = auth_client.post('/ship_order', json={
            'so_number': 'BAD ORDER NAME!',
            'box_id': '1',
            'weight': 1.5,
        })
        data = resp.get_json()
        assert data['status'] == 'error'
        assert 'invalid' in data['message'].lower()
        mock_backend.get_order.assert_not_called()


class TestBannedDestinationGate:
    """v0.7.0: a destination on the SettingsStore banned-country list
    must be rejected before any ShipRush or printer work. The seeded
    OFAC defaults are CU / IR / KP / SY."""

    def _order_to(self, country):
        return OrderData(
            so_number='SO-INTL', external_id='ext-banned',
            status='PACKED', warehouse_id=1, shippable=True,
            shippable_from_statuses=['PACKED'],
            items=[OrderItem(external_id='1', sku='X', display_name='X',
                             upc=None, qty=1)],
            shipping_address=ShippingAddress(
                name='Recipient', line1='1 Foreign Way',
                city='Pyongyang', state='', postal_code='00000',
                country=country, phone='000-000-0000',
            ),
            customer_name='Recipient', customer_phone='000',
            ship_method='UPS Worldwide Saver',
            order_total=10.0, customer_shipping_paid=0.0,
        )

    def test_banned_destination_blocks_before_shiprush(
            self, auth_client, mock_backend, mock_shiprush):
        mock_backend.get_order.return_value = self._order_to('KP')
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO-INTL',
            'box_id': '1',
            'weight': 1.0,
        })
        data = resp.get_json()
        assert data['status'] == 'error'
        assert 'banned' in data['message'].lower() or 'block' in data['message'].lower()
        mock_shiprush.generate_label.assert_not_called()
        mock_backend.confirm_shipped.assert_not_called()

    def test_allowed_destination_does_not_trigger_gate(
            self, auth_client, mock_backend, mock_shiprush):
        # Canada is not on the OFAC seed list; ship flow proceeds.
        mock_backend.get_order.return_value = self._order_to('CA')
        mock_backend.confirm_shipped.return_value = ShipResult(
            status='SHIPPED', tracking='1Z999AA10123456784',
            shipped_at='2026-05-11T15:30:00Z',
            fulfillment_id=44, audit_log_id=9003,
        )
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO-INTL',
            'box_id': '1',
            'weight': 1.0,
        })
        data = resp.get_json()
        assert data['status'] == 'success'
        assert mock_shiprush.generate_label.called


class TestShippableStatusGate:
    """An order may only flow into dockd / be shipped when its status is
    in the Sentry allow-list (PICKED / PACKED). A non-shippable status is
    rejected at scan time and again before any ShipRush label is burned,
    so a tracking number is never orphaned by a confirm_shipped 410."""

    def test_load_order_blocks_non_shippable_status(self, auth_client, mock_backend):
        mock_backend.get_order.return_value = _sample_order(
            so_number='SO-OPEN', status='OPEN')
        resp = auth_client.get('/get_order_details?so_number=SO-OPEN')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'error'
        assert data['message'] == (
            'This order is OPEN, Must be PICKED or PACKED to be shipped.')

    def test_load_order_allows_picked(self, auth_client, mock_backend):
        mock_backend.get_order.return_value = _sample_order(
            so_number='SO-P', status='PICKED')
        data = auth_client.get('/get_order_details?so_number=SO-P').get_json()
        assert data['status'] == 'success'

    def test_load_order_allows_packed(self, auth_client, mock_backend):
        mock_backend.get_order.return_value = _sample_order(
            so_number='SO-P', status='PACKED')
        data = auth_client.get('/get_order_details?so_number=SO-P').get_json()
        assert data['status'] == 'success'

    def test_load_order_blocks_shipped_status(self, auth_client, mock_backend):
        # SHIPPED is not PICKED/PACKED, so it is blocked too (void uses
        # its own modal / endpoint, not this load path).
        mock_backend.get_order.return_value = _sample_order(
            so_number='SO-S', status='SHIPPED', shipped=True)
        data = auth_client.get('/get_order_details?so_number=SO-S').get_json()
        assert data['status'] == 'error'
        assert data['message'] == (
            'This order is SHIPPED, Must be PICKED or PACKED to be shipped.')

    def test_ship_order_blocks_before_shiprush_and_writeback(
            self, auth_client, mock_backend, mock_shiprush):
        # The orphaned-label fix: a non-shippable order must not reach
        # generate_label (which burns a tracking number) nor
        # confirm_shipped (which Sentry would 410).
        mock_backend.get_order.return_value = _sample_order(
            so_number='SO-OPEN', status='OPEN')
        resp = auth_client.post('/ship_order', json={
            'so_number': 'SO-OPEN',
            'box_id': '1',
            'weight': 1.5,
        })
        data = resp.get_json()
        assert data['status'] == 'error'
        assert data['message'] == (
            'This order is OPEN, Must be PICKED or PACKED to be shipped.')
        mock_shiprush.generate_label.assert_not_called()
        mock_backend.confirm_shipped.assert_not_called()


class TestManualLinkWithBackend:

    def test_manual_link_happy_path(self, auth_client, mock_backend):
        mock_backend.confirm_shipped.return_value = ShipResult(
            status='SHIPPED',
            tracking='1Z999AA10123456784',
            shipped_at='2026-05-11T15:30:00Z',
            fulfillment_id=43,
            audit_log_id=9002,
        )
        resp = auth_client.post('/manual_link_tracking', json={
            'so_number': 'SO-9',
            'tracking': '1Z999AA10123456784',
        })
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['sentry_fulfillment_id'] == 43
        call = mock_backend.confirm_shipped.call_args
        assert call.kwargs['manual_link'] is True
        # Carrier is inferred from the 1Z prefix.
        assert call.kwargs['carrier'] == 'UPS'


class TestVoidWithBackend:

    def test_void_calls_shiprush_then_backend(self, auth_client, mock_backend,
                                              mock_shiprush):
        # Seed a label in the cache so the void path can resolve the
        # ShipRush shipment_id.
        from flask import current_app
        with auth_client.application.app_context():
            current_app.shipping_service.label_cache.save(
                'SO-10', 'shiprush-shipment-123', '1Z999AA10123456784',
                'XlhBClRFU1QKXlha',
            )
        mock_backend.void_ship.return_value = VoidResult(
            status='PACKED',
            voided_at='2026-05-11T16:00:00Z',
            audit_log_id=9100,
        )
        resp = auth_client.post('/void_label', json={
            'so_number': 'SO-10',
            'reason': 'wrong box dims',
        })
        data = resp.get_json()
        assert data['status'] == 'success'
        assert mock_shiprush.void_label.called
        assert mock_backend.void_ship.called
        assert mock_backend.void_ship.call_args.kwargs['reason'] == 'wrong box dims'
