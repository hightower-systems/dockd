"""Shipping service - orchestrates the full ship-order workflow.

Coordinates: an order-source backend (Sentry, future), ShipRushClient,
CarrierEngine, PrinterService, LabelCache, and the shipping history
database.

The NetSuite-direct integration was removed in the v1.0 -> Sentry
migration; see dockd-plans/netsuite-legacy/ for the preserved
implementation and re-introduction notes.
"""

import json
import base64
import logging
from datetime import datetime

from app.models.database import get_ship_db, get_override_db
from app.services.validation import validate_ticket

logger = logging.getLogger('dockd.shipping')


_BACKEND_NOT_WIRED = (
    'Order backend not configured. Sentry integration is pending; '
    'load_order / ship_order / manual_link are unavailable until then.'
)


def user_friendly_error(exc_or_message, context=''):
    """Translate exceptions/messages into user-facing text."""
    msg = exc_or_message if isinstance(exc_or_message, str) else str(exc_or_message)
    msg_lower = msg.lower()
    if 'timeout' in msg_lower or 'timed out' in msg_lower:
        return 'Request timed out. Check your connection and try again.'
    if 'connection' in msg_lower or 'connect' in msg_lower:
        return 'Could not connect. Check network and try again.'
    if '401' in msg or 'unauthorized' in msg_lower or 'auth' in msg_lower:
        return 'Login or permission error. Check credentials.'
    if '404' in msg or 'not found' in msg_lower:
        return 'Order or record not found. Check the order number.'
    if '500' in msg or '502' in msg_lower or '503' in msg_lower:
        return 'Server is temporarily unavailable. Try again in a moment.'
    if 'scale' in msg_lower or 'hid' in msg_lower or 'device' in msg_lower:
        return 'Scale not connected or not responding. Check USB and try again.'
    if 'printer' in msg_lower or 'print' in msg_lower:
        return 'Could not send to printer. Check printer connection and path.'
    if 'file' in msg_lower and ('read' in msg_lower or 'open' in msg_lower):
        return 'Could not read saved label file. Try reprinting from a recent order.'
    if len(msg) > 200:
        return 'Something went wrong. Try again or contact support.'
    return msg or 'Something went wrong. Please try again.'


class ShippingService:

    def __init__(self, backend, shiprush, carrier_engine, printer,
                 label_cache, config):
        self.backend = backend
        self.shiprush = shiprush
        self.carrier = carrier_engine
        self.printer = printer
        self.label_cache = label_cache
        self.config = config
        self.ship_counts = {}  # {(username, "YYYY-MM-DD"): count}

    def load_order(self, ticket, selected_ff_id=None):
        """Load order details from the order backend.

        Until a backend is wired the route returns a structured error so
        the UI can surface a clear "backend not configured" state.
        """
        if self.backend is None:
            return {'status': 'error', 'message': _BACKEND_NOT_WIRED}
        # Placeholder for the Sentry-backed implementation. See
        # dockd-plans/sentry-dockd-integration-dockd-side.md.
        return {'status': 'error', 'message': _BACKEND_NOT_WIRED}

    def ship_order(self, fulfillment_id, box_id, weight, order_number,
                   carrier_override=None, ca_shipping_paid=0,
                   ob_dims=None, client_ip=None, user=None,
                   order_loaded_at=None, ff_created_at=None):
        """Execute the full ship flow against the order backend."""
        if self.backend is None:
            return {'status': 'error', 'message': _BACKEND_NOT_WIRED}
        return {'status': 'error', 'message': _BACKEND_NOT_WIRED}

    def reprint(self, ticket, client_ip):
        """Reprint a label from local cache. Does not touch the backend."""
        clean = validate_ticket(ticket)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        zpl_b64 = self.label_cache.get_zpl_b64(clean)
        if not zpl_b64:
            return {
                'status': 'error',
                'message': 'Label not found. Only labels from recent orders (last 8 hours) can be reprinted.',
            }

        try:
            station = self.printer.resolve_station(client_ip)
            zpl_bytes = base64.b64decode(zpl_b64)
            self.printer.send(station, zpl_bytes)
        except Exception as e:
            return {'status': 'error', 'message': user_friendly_error(e, 'reprint')}

        record = self.label_cache.lookup(clean)
        tracking = record.get('tracking', 'Unknown') if record else 'Unknown'
        return {'status': 'success', 'message': 'Label sent to printer', 'tracking': tracking}

    def void(self, ticket):
        """Void a ShipRush label. Does not touch the backend."""
        clean = validate_ticket(ticket)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        shipment_id = self.label_cache.get_shipment_id(clean)
        if not shipment_id:
            return {
                'status': 'error',
                'message': 'Order not found in local history. Cannot void this label.',
            }

        result = self.shiprush.void_label(shipment_id)
        if result.get('status') == 'error':
            result['message'] = user_friendly_error(result.get('message', ''), 'void')
        return result

    def manual_link(self, ticket, tracking, user):
        """Manually link a tracking number to an order in the backend."""
        if self.backend is None:
            return {'status': 'error', 'message': _BACKEND_NOT_WIRED}
        return {'status': 'error', 'message': _BACKEND_NOT_WIRED}

    def log_override(self, order_number, user, items, station, override_type):
        """Log a manual override to the local audit database."""
        try:
            conn = get_override_db()
            for item in items:
                conn.execute(
                    "INSERT INTO override_log "
                    "(order_number, user, item_name, sku, station, override_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (order_number, user, item.get('item_name', ''),
                     item.get('sku', ''), station, override_type),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Failed to log override: %s", e)

    def get_ship_count(self, user):
        today = datetime.now().strftime('%Y-%m-%d')
        return self.ship_counts.get((user, today), 0)

    def _log_to_db(self, order_number, fulfillment_id, ff_data,
                   effective_box_id, dims, weight, shipping_cost,
                   tracking, carrier_override, carrier_switched,
                   ship_method_raw, current_user, ff_created_at,
                   order_loaded_at):
        """Write shipping record to history database.

        Kept intact across the NetSuite -> Sentry transition: schema and
        callers are unchanged once the Sentry-backed ship_order path
        lands. Currently unused while ship_order is stubbed.
        """
        try:
            shipped_at = datetime.now()
            items_skus = json.dumps([
                {
                    'sku': (line.get('item', {}).get('refName', '').split(' ')[0]
                            if ' ' in line.get('item', {}).get('refName', '')
                            else str(line.get('item', {}).get('id', ''))),
                    'qty': abs(int(line.get('quantity', 1))),
                }
                for line in ff_data.get('item', {}).get('items', [])
            ])
            dims_str = f"{dims['l']}x{dims['w']}x{dims['h']}"
            final_carrier = carrier_override if carrier_switched else ship_method_raw

            fulfillment_age_minutes = None
            if ff_created_at:
                try:
                    ff_dt = datetime.fromisoformat(ff_created_at.replace('Z', '+00:00'))
                    fulfillment_age_minutes = round(
                        (shipped_at - ff_dt.astimezone().replace(tzinfo=None)).total_seconds() / 60, 1)
                except Exception:
                    pass

            ship_speed_seconds = None
            if order_loaded_at:
                try:
                    loaded_dt = datetime.fromisoformat(order_loaded_at.replace('Z', '+00:00'))
                    ship_speed_seconds = round(
                        (shipped_at - loaded_dt.astimezone().replace(tzinfo=None)).total_seconds(), 1)
                except Exception:
                    pass

            conn = get_ship_db()
            conn.execute(
                """INSERT INTO ship_history
                   (order_number, fulfillment_id, items_skus, box_id, dims, weight,
                    shipping_cost, tracking, carrier, ship_method, shipped_by, shipped_at,
                    ff_created_at, order_loaded_at, fulfillment_age_minutes,
                    ship_speed_seconds, carrier_switched)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_number, fulfillment_id, items_skus, effective_box_id,
                 dims_str, weight, shipping_cost, tracking, final_carrier,
                 ship_method_raw, current_user,
                 shipped_at.strftime('%Y-%m-%d %H:%M:%S'),
                 ff_created_at, order_loaded_at, fulfillment_age_minutes,
                 ship_speed_seconds, 1 if carrier_switched else 0),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Failed to log ship history: %s", e)
