"""Shipping service - orchestrates the full ship-order workflow.

Coordinates an order backend (Sentry-WMS in v0.2.0), ShipRushClient,
CarrierEngine, PrinterService, LabelCache, and the local
`ship_history` SQLite table.

Sentry is the source of truth for order data and the destination of
ship / void-ship writes. ShipRush is the label generator (untouched
by the Sentry migration). The carrier-optimization engine and the
printer service are local-only.
"""

import base64
import json
import logging
import re
import uuid
from datetime import datetime

from app.models.database import get_ship_db, get_override_db
from app.services.backend import (
    AlreadyShippedError,
    BackendError,
    IdempotencyLockTimeoutError,
    IdempotencyMismatchError,
    InvalidBodyError,
    NetworkError,
    NotFoundError,
    NotInShippableStatusError,
    NotShippedError,
    OrderData,
    RateLimitedError,
    UnknownOperatorError,
)
from app.services.ship_attempts import ShipAttemptsStore, new_idempotency_key
from app.services.validation import validate_ticket

logger = logging.getLogger('dockd.shipping')


_BACKEND_NOT_WIRED = (
    'Order backend not configured. Set BACKEND=sentry and SENTRY_BASE_URL '
    'to enable order loading and ship writeback.'
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


def _carrier_from_tracking(tracking):
    """Best-effort carrier inference from a tracking-number prefix.

    Used on the manual-link path where the operator types a tracking
    number but does not pick a carrier. Sentry requires `carrier` on
    every POST /ship body; an inferred value is sent and the operator
    can void + re-link if the inference is wrong.

    International carriers (DHL, Royal Mail, Canada Post, Aramex)
    are recognized by their published label prefixes; ambiguous
    numerics still resolve to 'UNKNOWN' so the operator can override.
    """
    t = (tracking or '').strip().upper()
    if not t:
        return 'UNKNOWN'
    if t.startswith('1Z'):
        return 'UPS'
    # USPS labels typically 20-22 digits starting with 9 (domestic)
    # or alpha+digits like LM/LN/CP/RA for international.
    if re.match(r'^9\d{19,21}$', t):
        return 'USPS'
    # USPS / UPU International S10 format: 2 letters + 9 digits + 2
    # letters (e.g., LM123456789US, RA987654321CA). The trailing
    # country code identifies the origin; carrier semantics depend on
    # who handed it off. CP/EE/EA/EC/RA/RC/RR/CV are typical Royal
    # Mail / Canada Post / USPS international handoffs.
    if re.match(r'^[A-Z]{2}\d{9}[A-Z]{2}$', t):
        prefix = t[:2]
        if prefix in {'CP', 'LM', 'LN', 'LP', 'RA', 'RB', 'RC', 'RR', 'RU', 'RX'}:
            return 'USPS_INTL'
        if prefix in {'LX', 'LE', 'LF'}:
            return 'ROYAL_MAIL'
        if prefix in {'EA', 'EE', 'EC'}:
            return 'CANADA_POST'
        return 'INTL'
    # DHL Express: 10 digits, no other shape that long; DHL eCommerce
    # uses GM + digits (mostly US returns).
    if re.match(r'^\d{10}$', t):
        return 'DHL'
    if t.startswith('GM') and re.match(r'^GM\d+$', t):
        return 'DHL_ECOMMERCE'
    # FedEx labels are 12 or 15 digit numerics; cannot reliably
    # disambiguate from USPS without a service code, so default to
    # "UNKNOWN" for short all-digit strings.
    return 'UNKNOWN'


def _normalize_country(country):
    """Return uppercase ISO 3166 alpha-2 or 'US' if missing/blank.

    Centralized so payload-building and the banned-country gate use
    identical normalization rules; otherwise a 'us' / 'usa' / ' US '
    skew between the two could let a banned destination through.
    """
    if not country:
        return 'US'
    return str(country).strip().upper()[:2] or 'US'


def _is_international(order):
    """True when the destination is non-US."""
    return _normalize_country(order.shipping_address.country) != 'US'


def _build_customs_items(order):
    """Project order items into the customs dict shape shiprush.py
    expects. Returns [] for orders without per-item customs metadata
    so domestic flows do not pay the cost."""
    out = []
    for it in order.items:
        c = it.customs
        if not c:
            continue
        out.append({
            'description': c.description or it.display_name or it.sku,
            'hs_code': c.hs_code or '',
            'country_of_origin': c.country_of_origin or '',
            'qty': it.qty,
            'unit_weight_oz': c.unit_weight_oz or 0,
            'unit_value': c.unit_value or 0,
        })
    return out


def _build_shiprush_payload(order):
    """Adapt an OrderData into the dict shape ShipRushClient expects.

    ShipRushClient was written against the legacy NetSuite item-
    fulfillment shape; this adapter stays inside ShippingService so
    the ShipRush client itself remains backend-agnostic.
    """
    addr = order.shipping_address
    return {
        'tranId': order.so_number,
        'shippingAddress': {
            'addressee': addr.name or order.customer_name or '',
            'addr1': addr.line1 or '',
            'addr2': addr.line2 or '',
            'city': addr.city or '',
            'state': addr.state or '',
            'zip': addr.postal_code or '',
            'country': _normalize_country(addr.country),
            'addrPhone': addr.phone or order.customer_phone or '',
        },
        'entity': {'refName': order.customer_name or 'Valued Customer'},
        'shipMethod': {'refName': order.ship_method or ''},
        # International (v0.7.0): the customs / currency / duty-payer
        # fields are read by ShipRushClient when the destination is
        # non-US. Empty / 'USD' / None for domestic orders so the
        # ShipRush XML stays bit-for-bit unchanged on the 95% path.
        'customs_items': _build_customs_items(order),
        'currency': order.currency or 'USD',
        'duties_paid_by': order.duties_paid_by,
        # Legacy keys ShipRushClient does not consume but other parts
        # of the legacy _log_to_db path read; kept empty here for
        # backward compatibility within ShippingService.
        'item': {'items': []},
        'package': {'items': []},
    }


def _order_to_load_dict(order):
    """Build the response dict the operator UI consumes today.

    Mirrors the v0.1.0 NetSuite-shaped return shape so frontend
    changes stay minimal. Renames `fulfillment_id` to `so_number`
    (the new primary identifier); `amazon_order_id` becomes an
    empty string (deprecated, kept for back-compat until the
    frontend stops reading it).
    """
    addr = order.shipping_address
    items = [
        {
            'internal_id': it.external_id,
            'sku': it.sku,
            'display_name': it.display_name,
            'upc': it.upc or '',
            'qty': it.qty,
            'qty_ordered': it.qty_ordered,
        }
        for it in order.items
    ]
    payload = {
        'status': 'success',
        'so_number': order.so_number,
        'order_number': order.so_number,
        'ship_method': order.ship_method or '',
        'items': items,
        'address': {
            'name': addr.name or order.customer_name or '',
            'addr1': addr.line1 or '',
            'addr2': addr.line2 or '',
            'city': addr.city or '',
            'state': addr.state or '',
            'zip': addr.postal_code or '',
            'country': _normalize_country(addr.country),
            'phone': addr.phone or order.customer_phone or '',
        },
        'order_total': order.order_total if order.order_total is not None else 0.0,
        'ca_shipping_paid': order.customer_shipping_paid if order.customer_shipping_paid is not None else 0.0,
        'amazon_order_id': '',
        'ff_created_at': order.ff_created_at or '',
        'memo': order.memo or '',
        'marketplace': order.marketplace or '',
        'shippable': order.shippable,
        'shippable_from_statuses': order.shippable_from_statuses,
        # Already-shipped fields (frontend renders the void prompt
        # when status == 'SHIPPED'):
        'shipped_status': order.status,
        'shipped_by': order.shipped_by or '',
        'tracking_number': order.tracking_number or '',
        'carrier': order.carrier or '',
        'shipped_at': order.shipped_at or '',
        'station_label': order.station_label or '',
    }
    return payload


def _backend_error_to_message(exc, default='Could not load order.'):
    """Map a typed BackendError into a user-facing string."""
    if isinstance(exc, NotFoundError):
        return 'Order not found. Check the order number.'
    if isinstance(exc, AlreadyShippedError):
        existing = exc.details.get('existing_tracking') or '(unknown)'
        return f'Order already shipped. Tracking: {existing}'
    if isinstance(exc, NotInShippableStatusError):
        current = exc.details.get('current_status') or '(unknown)'
        allowed = ', '.join(exc.details.get('allowed_statuses') or [])
        return f'Order is in {current}; must be in {allowed} to ship.'
    if isinstance(exc, IdempotencyMismatchError):
        return 'Duplicate ship attempt with different details. Contact support.'
    if isinstance(exc, IdempotencyLockTimeoutError):
        return 'Another ship attempt is in progress. Try again in a moment.'
    if isinstance(exc, UnknownOperatorError):
        return 'Your account is not recognized by the upstream system.'
    if isinstance(exc, NotShippedError):
        return 'Order is not currently shipped; cannot void.'
    if isinstance(exc, RateLimitedError):
        return 'Too many requests. Slow down and try again.'
    if isinstance(exc, NetworkError):
        return 'Cannot reach the upstream system. Check your connection.'
    if isinstance(exc, BackendError):
        return exc.message or default
    return default


# Statuses dockd is allowed to load and ship. Sentry is the source of
# truth and sends the per-order allow-list in `shippable_from_statuses`;
# this is the fallback used only when that field is absent, so the gate
# never silently opens up.
_DEFAULT_SHIPPABLE_STATUSES = ('PICKED', 'PACKED')


def _shippable_status_error(order):
    """Return a user-facing message if `order` is not in a shippable
    status, else None.

    Mirrors Sentry's own status check so a non-shippable order is
    rejected at scan/ship time -- before any ShipRush label is
    generated -- instead of after, where a 410 from confirm_shipped
    would orphan a tracking number Sentry refuses to record.
    """
    allowed = [
        str(s).strip().upper()
        for s in (order.shippable_from_statuses or [])
        if str(s).strip()
    ] or list(_DEFAULT_SHIPPABLE_STATUSES)
    current = (order.status or '').strip().upper()
    if current in allowed:
        return None
    shown = (order.status or '').strip() or 'UNKNOWN'
    return f"This order is {shown}, Must be {' or '.join(allowed)} to be shipped."


def _classify_for_attempt(exc):
    """Return ('unknown' | 'rejected', error_kind, status_code).

    Maps backend exception types to the ship_attempts state machine:
    unknown = retryable on the next dockd restart;
    rejected = backend has spoken, do not retry blindly.
    """
    if isinstance(exc, (NetworkError, IdempotencyLockTimeoutError, RateLimitedError)):
        return 'unknown', exc.error_kind, exc.status_code
    if isinstance(exc, (AlreadyShippedError, NotInShippableStatusError,
                        UnknownOperatorError, IdempotencyMismatchError,
                        InvalidBodyError, NotFoundError, NotShippedError)):
        return 'rejected', exc.error_kind, exc.status_code
    if isinstance(exc, BackendError):
        # Generic / unmapped; default to unknown so a transient
        # backend-side issue gets a retry. If the error truly is
        # terminal the next retry will surface the same exception.
        return 'unknown', exc.error_kind, exc.status_code
    return 'unknown', 'unexpected', None


class ShippingService:

    def __init__(self, backend, shiprush, carrier_engine, printer,
                 label_cache, config, ship_attempts=None, settings=None):
        self.backend = backend
        self.shiprush = shiprush
        self.carrier = carrier_engine
        self.printer = printer
        self.label_cache = label_cache
        self.config = config
        self.ship_attempts = ship_attempts or ShipAttemptsStore()
        # SettingsStore is needed for the banned-country gate; fall
        # back to the carrier engine's reference (always set) so
        # legacy / test instantiations that omit the new kwarg keep
        # working without a forced refactor.
        self.settings = settings or getattr(carrier_engine, '_settings', None)
        self.ship_counts = {}  # {(username, "YYYY-MM-DD"): count}

    # ---- order load ----------------------------------------------------

    def load_order(self, so_number, selected_ff_id=None):
        """Fetch order details from the backend.

        `selected_ff_id` is accepted for backward compatibility with the
        legacy NetSuite-shaped frontend payload; it is ignored because
        the Sentry surface returns one SO per scan (no choice branch).
        """
        if self.backend is None:
            return {'status': 'error', 'message': _BACKEND_NOT_WIRED}

        clean = validate_ticket(so_number)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        try:
            order = self.backend.get_order(clean)
        except BackendError as exc:
            logger.info("load_order failed: %s %s", type(exc).__name__, exc.error_kind)
            return {'status': 'error', 'message': _backend_error_to_message(exc)}
        except Exception as exc:
            logger.error("load_order crash: %s", exc)
            return {'status': 'error', 'message': user_friendly_error(exc)}

        # Shippable-status gate: an order whose status Sentry will not
        # accept must not flow into the pack screen at all, so the
        # operator never packs an order that can't be shipped.
        status_msg = _shippable_status_error(order)
        if status_msg:
            logger.info(
                "load_order blocked: SO %s status %s not shippable",
                clean, order.status,
            )
            return {'status': 'error', 'message': status_msg}

        return _order_to_load_dict(order)

    # ---- ship ----------------------------------------------------------

    def ship_order(self, so_number, box_id, weight, order_number=None,
                   carrier_override=None, ca_shipping_paid=0,
                   ob_dims=None, client_ip=None, user=None,
                   order_loaded_at=None, ff_created_at=None,
                   idempotency_key=None,
                   station_id=None, station_label=None,
                   adult_signature=False):
        """Execute the full ship flow.

        Steps: refresh order from backend -> resolve box dims ->
        carrier conflict check -> apply carrier override -> generate
        ShipRush label -> print -> backend.confirm_shipped -> log to
        local SQLite.
        """
        if self.backend is None:
            return {'status': 'error', 'message': _BACKEND_NOT_WIRED}

        clean = validate_ticket(so_number)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        weight = max(float(weight or 0), 0.0625)
        box_id = str(box_id or '')

        try:
            order = self.backend.get_order(clean)
        except BackendError as exc:
            return {'status': 'error', 'message': _backend_error_to_message(exc)}

        # Shippable-status gate. Runs before any carrier / label work so
        # an order in a status Sentry will reject cannot burn a ShipRush
        # tracking number that confirm_shipped then refuses to record
        # (the orphaned-label / "did not update upstream" failure). The
        # status can change between load and ship, and a stale browser
        # can POST directly, so this re-check is the real enforcement;
        # the load_order gate is the operator-facing early warning.
        status_msg = _shippable_status_error(order)
        if status_msg:
            logger.warning(
                "Ship blocked: SO %s status %s not in shippable statuses",
                clean, order.status,
            )
            return {'status': 'error', 'message': status_msg}

        # International destination gate (v0.7.0). Runs before any
        # carrier / label work so a sanctioned destination cannot
        # consume ShipRush minutes, label inventory, or operator time.
        # Server-side enforcement is the source of truth; frontend
        # warnings are advisory only.
        dest_country = _normalize_country(order.shipping_address.country)
        if self.settings and self.settings.is_country_banned(dest_country):
            logger.warning(
                "Ship blocked: banned destination country %s for SO %s",
                dest_country, clean,
            )
            return {
                'status': 'error',
                'message': (
                    f'Shipping to {dest_country} is blocked by an active '
                    f'banned-destination rule. Contact compliance or update '
                    f'the rule in Settings -> International before retrying.'
                ),
            }

        ship_method_raw = (order.ship_method or '').strip()
        effective_box_id, dims, packaging_code = self.carrier.resolve_box_dims(
            box_id, ship_method_raw, ob_dims,
        )

        # Carrier conflict check (unless operator already overriding or
        # the order is already on FedEx).
        if not carrier_override:
            dest_zip = (order.shipping_address.postal_code or '')[:5]
            dest_addr1 = order.shipping_address.line1 or ''
            dest_addr2 = order.shipping_address.line2 or ''
            full_address = f"{dest_addr1} {dest_addr2}"
            conflict = self.carrier.check_carrier_conflict(
                box_id, dims, weight, ship_method_raw,
                dest_zip, full_address, float(ca_shipping_paid or 0),
                dest_country=dest_country,
            )
            if conflict:
                return conflict

        # Apply carrier override (unchanged carrier-engine logic).
        carrier_switched = False
        carrier_methods = self.carrier._carrier_methods()
        if carrier_override and carrier_override in carrier_methods:
            current = self.carrier.current_carrier(ship_method_raw)
            if carrier_override == 'FEDEX_ONE_RATE_2DAY':
                fedex_map = self.carrier._fedex_map()
                if box_id in fedex_map:
                    f = fedex_map[box_id]
                    dims = {'l': f['l'], 'w': f['w'], 'h': f['h']}
                    packaging_code = f['type']
                    effective_box_id = box_id
                else:
                    box_map = self.carrier._box_map()
                    scanned = box_map.get(box_id.upper())
                    if isinstance(scanned, dict) and 'l' in scanned:
                        effective_box_id, dims, packaging_code = \
                            self.carrier.best_fedex_one_rate_box(
                                scanned['l'], scanned['w'], scanned['h'])
                carrier_switched = True
                logger.info("Carrier override: %s -> FedEx One Rate 2 Day", ship_method_raw)
            elif current != carrier_override:
                carrier_switched = True
                logger.info("Carrier override: %s -> %s", ship_method_raw, carrier_override)

        # Generate label via ShipRush.
        ff_data = _build_shiprush_payload(order)
        result = self.shiprush.generate_label(
            ff_data, dims, weight, packaging_code, clean,
            box_id=effective_box_id, carrier_override=carrier_override,
            adult_signature=bool(adult_signature),
        )
        if result.get('status') == 'error':
            return {'status': 'error', 'message': user_friendly_error(result.get('message', ''))}

        tracking = result['tracking']
        zpl_b64 = result['zpl_b64']
        shipping_cost = result.get('cost')
        logger.info("Label generated, tracking: %s, cost: %s", tracking, shipping_cost)

        # Print flow flip (v0.3.0): the dockd container has no printer.
        # The browser receives `zpl_b64` in the success response and
        # forwards it to its local scale-agent at 127.0.0.1:5050/print.

        # Resolve the carrier name to send to Sentry. carrier_override
        # is a slot key like UPS / USPS / FEDEX_ONE_RATE_2DAY; without
        # an override, infer from the ship method.
        if carrier_switched:
            sentry_carrier = carrier_override
        else:
            sentry_carrier = self.carrier.current_carrier(ship_method_raw) or 'UNKNOWN'

        # Confirm ship on Sentry, persisting the attempt before the
        # network call so a crash mid-flight leaves a recoverable row.
        if not idempotency_key:
            idempotency_key = new_idempotency_key()
        operator_username = user or 'unknown'

        confirm_kwargs = {
            'tracking': tracking,
            'carrier': sentry_carrier,
            'ship_method': ship_method_raw or None,
            'operator_username': operator_username,
            'shipping_cost': shipping_cost,
            'weight': weight,
            'dims': dims,
            'manual_link': False,
            'idempotency_key': idempotency_key,
        }
        self.ship_attempts.insert_pending(
            idempotency_key=idempotency_key,
            operation='ship',
            so_number=clean,
            request_body={'so_number': clean, **confirm_kwargs},
        )

        try:
            ship_result = self.backend.confirm_shipped(clean, **confirm_kwargs)
        except BackendError as exc:
            state, error_kind, status_code = _classify_for_attempt(exc)
            if state == 'unknown':
                self.ship_attempts.mark_unknown(idempotency_key, error_kind, exc.message)
            else:
                self.ship_attempts.mark_rejected(
                    idempotency_key, error_kind, exc.message,
                    details=exc.details, response_status=status_code,
                )
            if isinstance(exc, AlreadyShippedError):
                logger.warning("Sentry says SO already shipped: %s", exc.details)
                return {
                    'status': 'error',
                    'message': (
                        f'Label printed but order is already shipped on the upstream '
                        f'system (tracking on file: {exc.details.get("existing_tracking", "?")}).'
                    ),
                }
            if state == 'rejected':
                logger.error(
                    "Sentry rejected ship write: %s %s",
                    type(exc).__name__, exc.error_kind,
                )
                return {
                    'status': 'error',
                    'message': (
                        f'Label printed but the upstream system rejected the ship: '
                        f'{_backend_error_to_message(exc)}. Tracking on the label: {tracking}'
                    ),
                }
            logger.error("Sentry ship write failed (unknown): %s", exc)
            return {
                'status': 'error',
                'message': (
                    f'Label printed but the upstream system did not confirm. '
                    f'Tracking on the label: {tracking}. Dockd will retry on its next restart.'
                ),
            }

        # Success path: record the response in ship_attempts so a
        # subsequent retry would short-circuit to the cached body.
        self.ship_attempts.mark_success(
            idempotency_key,
            response_body={
                'status': ship_result.status,
                'tracking': ship_result.tracking,
                'shipped_at': ship_result.shipped_at,
                'fulfillment_id': ship_result.fulfillment_id,
                'audit_log_id': ship_result.audit_log_id,
            },
        )
        logger.info("Sentry confirmed ship: fulfillment_id=%s audit=%s",
                    ship_result.fulfillment_id, ship_result.audit_log_id)

        # Local history. For international shipments, capture the
        # destination country + total declared customs value + the
        # comma-separated HS codes so historical audits do not have
        # to re-query Sentry to reconstruct what got declared.
        customs_value_total = None
        customs_currency_log = None
        hs_codes_log = None
        if dest_country != 'US':
            customs_items_log = ff_data.get('customs_items') or []
            customs_value_total = self.shiprush._sum_customs_value(customs_items_log) or None
            customs_currency_log = ff_data.get('currency') or 'USD'
            hs_codes_log = ','.join(
                str(ci.get('hs_code') or '').strip()
                for ci in customs_items_log if ci.get('hs_code')
            ) or None
        self._log_to_db(
            order_number=clean, fulfillment_id=str(ship_result.fulfillment_id),
            ff_data=ff_data, effective_box_id=effective_box_id,
            dims=dims, weight=weight, shipping_cost=shipping_cost,
            tracking=tracking, carrier_override=carrier_override,
            carrier_switched=carrier_switched, ship_method_raw=ship_method_raw,
            current_user=operator_username, ff_created_at=ff_created_at,
            order_loaded_at=order_loaded_at,
            station_id=station_id, station_label=station_label,
            external_id=order.external_id,
            customer_shipping_paid=order.customer_shipping_paid,
            order_total=order.order_total,
            sentry_audit_log_id=ship_result.audit_log_id,
            sentry_fulfillment_id=ship_result.fulfillment_id,
            manual_link=False,
            idempotency_key=idempotency_key,
            destination_country=dest_country,
            customs_value=customs_value_total,
            customs_currency=customs_currency_log,
            hs_codes=hs_codes_log,
        )

        today = datetime.now().strftime('%Y-%m-%d')
        key = (operator_username, today)
        self.ship_counts[key] = self.ship_counts.get(key, 0) + 1

        return {
            'status': 'success',
            'tracking': tracking,
            'carrier_switched': carrier_switched,
            'sentry_fulfillment_id': ship_result.fulfillment_id,
            'sentry_audit_log_id': ship_result.audit_log_id,
            # The browser forwards this to its scale agent at
            # 127.0.0.1:5050/print after the success response lands.
            'zpl_b64': zpl_b64,
        }

    # ---- reprint -------------------------------------------------------

    def reprint(self, ticket, client_ip=None):
        """Look up a label from local cache and return its ZPL.

        The browser forwards the returned `zpl_b64` to its local
        scale-agent for printing. No server-side print call.
        """
        clean = validate_ticket(ticket)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        zpl_b64 = self.label_cache.get_zpl_b64(clean)
        if not zpl_b64:
            return {
                'status': 'error',
                'message': 'Label not found. Only labels from recent orders (last 8 hours) can be reprinted.',
            }

        record = self.label_cache.lookup(clean)
        tracking = record.get('tracking', 'Unknown') if record else 'Unknown'
        return {
            'status': 'success',
            'message': 'Label ready',
            'tracking': tracking,
            'zpl_b64': zpl_b64,
        }

    # ---- void ----------------------------------------------------------

    def void(self, ticket, *, reason=None, operator_username=None,
             idempotency_key=None):
        """Void a ShipRush label and the corresponding ship on the backend.

        Order of operations:
          1. ShipRush void (refunds the label).
          2. backend.void_ship (reverts the SO to its pre-ship status).

        If ShipRush succeeds and backend.void_ship fails, the operator
        sees a clear "label refunded but upstream not reverted" error
        with the tracking number; they retry with the same idempotency
        key.
        """
        clean = validate_ticket(ticket)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        shipment_id = self.label_cache.get_shipment_id(clean)
        if not shipment_id:
            return {
                'status': 'error',
                'message': 'Order not found in local history. Cannot void this label.',
            }

        sr_result = self.shiprush.void_label(shipment_id)
        if sr_result.get('status') == 'error':
            sr_result['message'] = user_friendly_error(sr_result.get('message', ''), 'void')
            return sr_result

        # ShipRush refund succeeded. Reverse on the backend too.
        if self.backend is None:
            sr_result.setdefault(
                'message',
                'Label voided locally. Upstream system not configured.',
            )
            return sr_result

        if not idempotency_key:
            idempotency_key = new_idempotency_key()
        void_kwargs = {
            'reason': reason or 'voided via dockd',
            'operator_username': operator_username or 'unknown',
            'idempotency_key': idempotency_key,
        }
        self.ship_attempts.insert_pending(
            idempotency_key=idempotency_key,
            operation='void',
            so_number=clean,
            request_body={'so_number': clean, **void_kwargs},
        )

        try:
            void_result = self.backend.void_ship(clean, **void_kwargs)
        except NotShippedError as exc:
            # Sentry says it's already not in SHIPPED state -- somebody
            # else already voided. Treat as success (ShipRush refunded;
            # backend already in the desired state).
            self.ship_attempts.mark_success(
                idempotency_key,
                response_body={'status': 'already_voided', 'message': exc.message},
            )
            return {
                'status': 'success',
                'message': 'Label voided. Upstream order was already reverted.',
            }
        except BackendError as exc:
            state, error_kind, status_code = _classify_for_attempt(exc)
            if state == 'unknown':
                self.ship_attempts.mark_unknown(idempotency_key, error_kind, exc.message)
            else:
                self.ship_attempts.mark_rejected(
                    idempotency_key, error_kind, exc.message,
                    details=exc.details, response_status=status_code,
                )
            logger.error("backend void_ship failed: %s", exc)
            return {
                'status': 'error',
                'message': (
                    f'Label refund succeeded, but upstream system did not revert: '
                    f'{_backend_error_to_message(exc)}. Retry the void.'
                ),
            }

        self.ship_attempts.mark_success(
            idempotency_key,
            response_body={
                'status': void_result.status,
                'voided_at': void_result.voided_at,
                'audit_log_id': void_result.audit_log_id,
            },
        )
        try:
            self._mark_history_voided(clean, void_result.voided_at, reason)
        except Exception as e:
            logger.warning("Could not mark ship_history voided for %s: %s", clean, e)
        return {
            'status': 'success',
            'message': sr_result.get('message') or 'Label voided.',
            'reverted_to_status': void_result.status,
            'sentry_audit_log_id': void_result.audit_log_id,
        }

    # ---- manual link ---------------------------------------------------

    def manual_link(self, ticket, tracking, user, *, carrier=None,
                    ship_method=None, idempotency_key=None):
        """Manually attach an operator-provided tracking number to an
        order on the backend (no ShipRush label generated)."""
        if self.backend is None:
            return {'status': 'error', 'message': _BACKEND_NOT_WIRED}

        clean = validate_ticket(ticket)
        if not clean:
            return {'status': 'error', 'message': 'Invalid order number format'}

        tracking = str(tracking).strip()
        if not tracking:
            return {'status': 'error', 'message': 'Tracking number is required'}
        logger.info("Manual link: order %s -> tracking %s", clean, tracking)

        if not idempotency_key:
            idempotency_key = new_idempotency_key()

        confirm_kwargs = {
            'tracking': tracking,
            'carrier': (carrier or _carrier_from_tracking(tracking)),
            'ship_method': ship_method,
            'operator_username': user or 'unknown',
            'shipping_cost': None,
            'weight': None,
            'dims': None,
            'manual_link': True,
            'idempotency_key': idempotency_key,
        }
        self.ship_attempts.insert_pending(
            idempotency_key=idempotency_key,
            operation='manual_link',
            so_number=clean,
            request_body={'so_number': clean, **confirm_kwargs},
        )

        try:
            ship_result = self.backend.confirm_shipped(clean, **confirm_kwargs)
        except BackendError as exc:
            state, error_kind, status_code = _classify_for_attempt(exc)
            if state == 'unknown':
                self.ship_attempts.mark_unknown(idempotency_key, error_kind, exc.message)
            else:
                self.ship_attempts.mark_rejected(
                    idempotency_key, error_kind, exc.message,
                    details=exc.details, response_status=status_code,
                )
            return {'status': 'error', 'message': _backend_error_to_message(exc)}

        self.ship_attempts.mark_success(
            idempotency_key,
            response_body={
                'status': ship_result.status,
                'tracking': ship_result.tracking,
                'shipped_at': ship_result.shipped_at,
                'fulfillment_id': ship_result.fulfillment_id,
                'audit_log_id': ship_result.audit_log_id,
            },
        )
        return {
            'status': 'success',
            'message': f'Linked {clean} to {tracking}',
            'sentry_fulfillment_id': ship_result.fulfillment_id,
            'sentry_audit_log_id': ship_result.audit_log_id,
        }

    def retry_recoverable_attempts(self, *, limit=50):
        """Drain pending / unknown rows from ship_attempts.

        Called from the app factory at boot (gated on the
        DOCKD_RETRY_PENDING_ON_BOOT env flag) to recover from a
        process crash that left rows mid-flight. Idempotency-safe
        by construction: Sentry's dockd_idempotency table replays
        the cached response when the same key + body has already
        committed there, or re-executes the write when it hasn't.

        Returns a list of `(idempotency_key, new_status)` tuples
        for logging.
        """
        if self.backend is None:
            return []
        results = []
        rows = self.ship_attempts.find_recoverable(limit=limit)
        for row in rows:
            try:
                body = json.loads(row['request_body'])
            except Exception:
                logger.warning(
                    "Skipping unparseable ship_attempts row %s",
                    row.get('idempotency_key'),
                )
                continue
            op = row['operation']
            key = row['idempotency_key']
            so_number = row['so_number']
            try:
                if op == 'ship' or op == 'manual_link':
                    self.backend.confirm_shipped(
                        so_number,
                        tracking=body.get('tracking'),
                        carrier=body.get('carrier'),
                        ship_method=body.get('ship_method'),
                        operator_username=body.get('operator_username') or 'unknown',
                        shipping_cost=body.get('shipping_cost'),
                        weight=body.get('weight'),
                        dims=body.get('dims'),
                        manual_link=bool(body.get('manual_link', op == 'manual_link')),
                        idempotency_key=key,
                    )
                elif op == 'void':
                    self.backend.void_ship(
                        so_number,
                        reason=body.get('reason') or 'voided via dockd',
                        operator_username=body.get('operator_username') or 'unknown',
                        idempotency_key=key,
                    )
                else:
                    logger.warning("Unknown operation %r in ship_attempts row %s", op, key)
                    continue
            except BackendError as exc:
                state, error_kind, status_code = _classify_for_attempt(exc)
                if state == 'unknown':
                    self.ship_attempts.mark_unknown(key, error_kind, exc.message)
                else:
                    self.ship_attempts.mark_rejected(
                        key, error_kind, exc.message,
                        details=exc.details, response_status=status_code,
                    )
                results.append((key, state))
                logger.info(
                    "Retry %s -> %s (%s)", key[:8], state, error_kind,
                )
                continue
            self.ship_attempts.mark_success(key)
            results.append((key, 'success'))
            logger.info("Retry %s -> success", key[:8])
        return results

    def _mark_history_voided(self, so_number, voided_at, reason):
        """Stamp the matching ship_history row as voided.

        Called from `void()` after the backend confirms the reversal
        so the local audit trail matches the upstream state. Best-
        effort; an exception here does not fail the void.
        """
        conn = get_ship_db()
        try:
            conn.execute(
                """UPDATE ship_history
                      SET voided_at = ?, void_reason = ?
                    WHERE order_number = ?
                      AND voided_at IS NULL""",
                (voided_at or datetime.now().isoformat(),
                 (reason or 'voided via dockd')[:1000],
                 so_number),
            )
            conn.commit()
        finally:
            conn.close()

    # ---- audit / stats -------------------------------------------------

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
                   order_loaded_at, station_id=None, station_label=None,
                   external_id=None, customer_shipping_paid=None,
                   order_total=None, sentry_audit_log_id=None,
                   sentry_fulfillment_id=None, manual_link=False,
                   idempotency_key=None, destination_country=None,
                   customs_value=None, customs_currency=None,
                   hs_codes=None):
        """Write shipping record to local history database."""
        try:
            shipped_at = datetime.now()
            # Adapt the Sentry-style items list (already shape-shifted
            # by _build_shiprush_payload) into the legacy SKU/qty log.
            items_skus = json.dumps([])
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
                    ship_speed_seconds, carrier_switched, station_id, station_label,
                    external_id, customer_shipping_paid, order_total,
                    sentry_audit_log_id, sentry_fulfillment_id, manual_link,
                    idempotency_key, destination_country, customs_value,
                    customs_currency, hs_codes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_number, fulfillment_id, items_skus, effective_box_id,
                 dims_str, weight, shipping_cost, tracking, final_carrier,
                 ship_method_raw, current_user,
                 shipped_at.strftime('%Y-%m-%d %H:%M:%S'),
                 ff_created_at, order_loaded_at, fulfillment_age_minutes,
                 ship_speed_seconds, 1 if carrier_switched else 0,
                 station_id or '', station_label or '',
                 external_id or '', customer_shipping_paid, order_total,
                 sentry_audit_log_id, sentry_fulfillment_id,
                 1 if manual_link else 0, idempotency_key,
                 destination_country, customs_value, customs_currency,
                 hs_codes),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Failed to log ship history: %s", e)
