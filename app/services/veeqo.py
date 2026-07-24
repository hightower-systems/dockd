"""Veeqo Rate Shopping API client -- Amazon Buy Shipping for marketplace orders.

Veeqo is Amazon's free multi-carrier shipping platform. For an order that
originated on Amazon (Sentry `marketplace == "AMAZON"`), buying the label
through Veeqo's Rate Shopping API means the label is Amazon-verified: it
carries Buy Shipping protection (`protected: true`, e.g. CLAIMS_PROTECTED /
OTDR_PROTECTED) and booking auto-syncs tracking to Seller Central and marks
the Amazon order shipped. A label bought through ShipRush for the same order
gets none of that.

Role in dockd (decided 2026-07-24): Veeqo is offered *alongside* ShipRush at
the box scan for Amazon orders -- the operator picks the source per order.
So rate_shop() returns rate rows in the SAME shape ShipRushClient.rate_shop()
returns, tagged `source='veeqo'` and carrying the identifiers a booking needs
(`rate_id` + `remote_shipment_id` + `request_token`), so both carriers render
in one modal and the ship endpoint can tell which engine a chosen row belongs
to.

Wire contract mirrored from the local API reference at
~/hightower-systems/veeqo-rate-shop-api/ (get-rates, book-shipment,
cancel-shipment, get-label), OpenAPI 3.1.1 / API v1.0.0:

    Rates   POST   /shipping/api/v1/rates              -> quotes[] + remote_shipment_id + request_token
    Book    POST   /shipping/api/v1/shipments          -> successful{}/failed{} keyed by remote_shipment_id
    Cancel  DELETE /shipping/api/v1/shipments/{id}      -> 204; {id} is the remote_shipment_id

Auth (private integration): `x-api-key: <key>`, host `https://api.veeqo.com`,
key from the `VEEQO_TOKEN` env var (SHIPRUSH_TOKEN convention).

Grounding status: the RATES path is verified live against the AvidMax account
(2026-07-24, read-only) -- host, auth, and the quotes[] response shape all
confirmed, 34 services parsed on a domestic lane. Two live corrections the
docs got wrong are baked in below: `customer_reference` is required on EVERY
call (not just Amazon), and an Amazon call needs real Amazon OrderItemIds in
`channel_items[].remote_id` (a fabricated id is rejected by Amazon Shipping at
rate time). The BOOK and CANCEL paths still mirror the doc only -- a live book
costs a real label, so they are exercised via the stub / dry-run until an
end-to-end test on a real order. Rate limit is 5 req/s leaky bucket; do not
hammer from tests.
"""

import os
import logging

import requests

logger = logging.getLogger('dockd.veeqo')


class VeeqoClient:

    # Rate shopping sits in the ship critical path with an operator at the
    # bench, so it is kept tight like the ShipRush rate timeout: a slow quote
    # must never stall a ship. On any failure the ship flow prompts the
    # operator (retry Veeqo / fall back to ShipRush) rather than blocking.
    RATE_TIMEOUT = 12
    BOOK_TIMEOUT = 25
    VOID_TIMEOUT = 20

    DEFAULT_BASE_URL = 'https://api.veeqo.com'
    RATES_PATH = '/shipping/api/v1/rates'
    SHIPMENTS_PATH = '/shipping/api/v1/shipments'

    # dockd prints ZPL to the browser's local scale-agent, so book labels in
    # ZPL directly rather than converting a PDF at the bench.
    LABEL_FORMAT = 'ZPL'

    def __init__(self, settings_store, label_cache):
        self._settings = settings_store
        self.label_cache = label_cache

    # ---- env-backed credentials ----------------------------------------

    @property
    def token(self):
        # VEEQO_TOKEN matches the repo's SHIPRUSH_TOKEN convention; the older
        # VEEQO_API_KEY is accepted as a fallback so an install that set that
        # name keeps working.
        return (os.environ.get('VEEQO_TOKEN')
                or os.environ.get('VEEQO_API_KEY') or '').strip()

    @property
    def base_url(self):
        return (os.environ.get('VEEQO_BASE_URL') or self.DEFAULT_BASE_URL).rstrip('/')

    @property
    def configured(self):
        """True when a key is present. When False the ship flow simply never
        offers a Veeqo option -- Amazon orders still ship via ShipRush, so an
        unprovisioned install degrades to exactly today's behaviour."""
        return bool(self.token)

    @property
    def dry_run(self):
        """True when label BOOKS are stubbed out. Rating is unaffected.

        Same knob and semantics as ShipRushClient.dry_run: a realistic test
        rig can hold a real key and pull free, read-only rates while a single
        click in the modal must not book a real billable label. Unset or
        malformed reads as False so production cannot acquire it by accident.
        """
        return (os.environ.get('DOCKD_DRY_RUN_LABELS') or '').strip().lower() in (
            '1', 'true', 'yes', 'on')

    def _headers(self):
        return {
            'x-api-key': self.token,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _shipper_origin(self):
        return self._settings.get('shipper_origin', {}) or {}

    def _fallback_phone(self):
        return self._settings.get('fallback_customer_phone', '') or ''

    # ---- rate shopping (read-only) -------------------------------------

    def rate_shop(self, delivery_address, dims, weight_lbs, *,
                  customer_reference=None, amazon_order_id=None,
                  channel_items=None, estimated_value=None, currency='USD'):
        """Quote every Veeqo service for one package. Buys nothing.

        `amazon_order_id` becomes `customer_reference` and sets
        `is_amazon_order`; together with `channel_items[].remote_id` (the
        Amazon OrderItemIds) that is what makes Buy Shipping protection
        (`protected: true`) available and lets a later book auto-sync tracking
        to Seller Central. Without them Veeqo still quotes, but the rates are
        not Amazon-protected.

        Returns {'status': 'success', 'services': [...]} sorted cheapest
        first. Each row is shaped like ShipRushClient.rate_shop()'s rows plus
        `source='veeqo'`, and carries the three identifiers a book needs --
        `rate_id`, `remote_shipment_id`, `request_token` -- so a chosen row is
        self-sufficient. On any failure returns {'status': 'error',
        'message': ...}; callers treat that as "no Veeqo quote" and never as a
        reason to block a ship.
        """
        if not self.configured:
            return {'status': 'error', 'message': 'Veeqo is not configured.'}

        payload = self._rate_payload(
            delivery_address, dims, weight_lbs,
            customer_reference=customer_reference, amazon_order_id=amazon_order_id,
            channel_items=channel_items, estimated_value=estimated_value,
            currency=currency,
        )
        try:
            resp = requests.post(
                f'{self.base_url}{self.RATES_PATH}',
                json=payload, headers=self._headers(), timeout=self.RATE_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Veeqo rate request failed: %s", exc)
            return {'status': 'error', 'message': self.friendly_error(exc)}
        if resp.status_code != 200:
            logger.warning("Veeqo rate HTTP %s: %s", resp.status_code, resp.text[:400])
            return {'status': 'error', 'message': self.friendly_error(resp.text)}
        return self._parse_rates(resp)

    def _rate_payload(self, delivery_address, dims, weight_lbs, *,
                      customer_reference=None, amazon_order_id=None,
                      channel_items=None, estimated_value=None, currency='USD'):
        """Build a get-rates request body.

        Weight is sent in ounces because AvidMax is a sub-pound shipper (70%
        of parcels under a quarter pound); ounces keep small-parcel rounding
        honest. Dimensions are inches. Address fields follow Veeqo's schema
        (`line1`/`town`/`postcode`/`county`/`country_code`), which is NOT the
        ShipRush shape -- do not copy field names between the two builders.

        `customer_reference` is REQUIRED on every call (the live API 400s
        without it, though the docs mark it optional). For an Amazon order it
        MUST be the Amazon order number; otherwise it is the SO number.
        """
        origin = self._shipper_origin()
        addr = delivery_address or {}
        dims = dims or {}
        weight_oz = max(float(weight_lbs or 0) * 16.0, 0.01)

        body = {
            'from_address': {
                'name': origin.get('company', '') or 'AvidMax',
                'company': origin.get('company', ''),
                'phone': origin.get('phone', ''),
                'line1': origin.get('address1', ''),
                'line2': origin.get('address2', ''),
                'town': origin.get('city', ''),
                'postcode': origin.get('postal_code', ''),
                'county': origin.get('state', ''),
                'country_code': origin.get('country', 'US'),
            },
            'to_address': {
                'name': addr.get('addressee') or addr.get('name') or 'Valued Customer',
                'phone': addr.get('addrPhone') or self._fallback_phone(),
                'line1': addr.get('addr1', ''),
                'line2': addr.get('addr2', ''),
                'town': addr.get('city', ''),
                'postcode': addr.get('zip', ''),
                'county': addr.get('state', ''),
                'country_code': str(addr.get('country') or 'US').upper()[:2],
            },
            'parcels': [{
                'weight': round(weight_oz, 2),
                'weight_unit': 'oz',
                'length': dims.get('l') or None,
                'width': dims.get('w') or None,
                'height': dims.get('h') or None,
                'dimension_unit': 'in',
            }],
        }
        # customer_reference is mandatory. For Amazon it is the order number;
        # otherwise the caller-supplied SO reference, with a last-resort
        # placeholder so a rate lookup never 400s on a missing reference.
        body['customer_reference'] = str(
            amazon_order_id or customer_reference or 'DOCKD-RATE')
        if amazon_order_id:
            # is_amazon_order unlocks Buy Shipping protection but then makes
            # channel_items[].remote_id (Amazon OrderItemIds) mandatory.
            body['is_amazon_order'] = True
        if channel_items:
            body['channel_items'] = channel_items
        if estimated_value is not None:
            # Veeqo wants a string decimal here.
            body['estimated_value'] = f'{float(estimated_value):.2f}'
            body['currency_code'] = currency
        return body

    def _parse_rates(self, resp):
        try:
            data = resp.json()
        except ValueError:
            logger.warning("Veeqo rates: non-JSON body: %s", resp.text[:300])
            return {'status': 'error', 'message': 'Veeqo returned an unreadable rate response.'}
        if not isinstance(data, dict):
            return {'status': 'error', 'message': 'Veeqo returned an unexpected rate response.'}

        # Shipment-level identifiers every book of a row from this response
        # needs. Threaded onto each row so a chosen row is self-sufficient.
        remote_shipment_id = data.get('remote_shipment_id') or ''
        request_token = data.get('request_token') or ''
        expires_at = data.get('expires_at') or ''

        quotes = data.get('quotes')
        if not isinstance(quotes, list) or not quotes:
            return {'status': 'error', 'message': 'No Veeqo rates were returned for this package.'}

        services = []
        for q in quotes:
            if not isinstance(q, dict):
                continue
            total = _num(q.get('total_charge'))
            if total is None:
                total = _num(q.get('base_rate'))
            if total is None:
                # A rate with no price is not a choice at the bench.
                continue
            services.append({
                'source': 'veeqo',
                'name': q.get('service_name') or 'Veeqo service',
                'service_code': q.get('service_id') or q.get('carrier') or '',
                'carrier': q.get('carrier_nice_name') or q.get('carrier') or '',
                'total': total,
                # Veeqo gives a delivery DATE, not a day count. Carry the date
                # and the promise flag; day-count derivation for the
                # "no slower than ordered" rule is a follow-on if Veeqo ever
                # feeds the auto-selector (today the operator picks Veeqo rows).
                'transit_days': None,
                'expected_delivery': q.get('delivery_date') or '',
                'meets_promise': bool(q.get('meets_delivery_promise')),
                'protected': bool(q.get('protected')),
                'protections': q.get('protections') or [],
                'currency': q.get('currency_code') or 'USD',
                'rate_id': q.get('rate_id') or '',
                'remote_shipment_id': remote_shipment_id,
                'request_token': request_token,
                'expires_at': expires_at,
            })

        if not services:
            return {'status': 'error', 'message': 'No priced Veeqo rates were returned.'}

        services.sort(key=lambda s: s['total'])
        return {
            'status': 'success',
            'services': services,
            'remote_shipment_id': remote_shipment_id,
            'request_token': request_token,
            'expires_at': expires_at,
        }

    # ---- label buy / void ----------------------------------------------

    def buy_label(self, rate_id, remote_shipment_id, *,
                  request_token=None, order_number=None):
        """Book the shipment for a chosen quote and return its label.

        Booking needs BOTH the quote's `rate_id` and the shipment-level
        `remote_shipment_id` from the same rates response (and optionally the
        `request_token`); all three ride on the rate row the operator picked.
        The `remote_shipment_id` is also the void key, so it is what gets
        cached against the order.

        Returns {'status': 'success', 'tracking', 'zpl_b64', 'cost',
        'shipment_id'} on success (shape matches ShipRushClient.generate_label
        so the ship flow treats both engines the same; `shipment_id` is the
        remote_shipment_id for a later void), or {'status': 'error',
        'message': ...}.

        Honours dry_run: a test rig with a real key can pull rates for free
        but must never book a real billable label.
        """
        if not self.configured:
            return {'status': 'error', 'message': 'Veeqo is not configured.'}
        if not rate_id or not remote_shipment_id:
            return {'status': 'error', 'message': 'Veeqo rate is missing its booking ids.'}
        if self.dry_run:
            logger.info("Veeqo dry run: skipping live book for rate %s / %s (order %s)",
                        rate_id, remote_shipment_id, order_number)
            if order_number:
                self.label_cache.save(order_number, remote_shipment_id, 'DRYRUN-VEEQO', '')
            return {
                'status': 'success',
                'tracking': 'DRYRUN-VEEQO',
                'zpl_b64': '',
                'cost': None,
                'shipment_id': remote_shipment_id,
            }

        body = {
            'label_format': self.LABEL_FORMAT,
            'shipments': [{
                'remote_shipment_id': remote_shipment_id,
                'rate_id': rate_id,
            }],
        }
        if request_token:
            body['request_token'] = request_token

        try:
            resp = requests.post(
                f'{self.base_url}{self.SHIPMENTS_PATH}',
                json=body, headers=self._headers(), timeout=self.BOOK_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Veeqo book failed: %s", exc)
            return {'status': 'error', 'message': self.friendly_error(exc)}
        if resp.status_code not in (200, 201):
            logger.warning("Veeqo book HTTP %s: %s", resp.status_code, resp.text[:400])
            return {'status': 'error', 'message': self.friendly_error(resp.text)}
        return self._parse_booking(resp, remote_shipment_id, order_number)

    def _parse_booking(self, resp, remote_shipment_id, order_number):
        try:
            data = resp.json()
        except ValueError:
            return {'status': 'error', 'message': 'Veeqo returned an unreadable booking response.'}
        if not isinstance(data, dict):
            return {'status': 'error', 'message': 'Veeqo returned an unexpected booking response.'}

        # Success/fail are maps keyed by remote_shipment_id.
        successful = data.get('successful') or {}
        failed = data.get('failed') or {}

        entry = successful.get(remote_shipment_id)
        if entry is None and len(successful) == 1:
            # Tolerate a key we did not expect: if exactly one shipment
            # succeeded, it is ours.
            entry = next(iter(successful.values()))

        if isinstance(entry, dict):
            tracking = entry.get('tracking_number') or ''
            label_b64 = entry.get('label_content') or ''
            cost = _num((entry.get('total_charge') or {}).get('value')) \
                if isinstance(entry.get('total_charge'), dict) else _num(entry.get('total_charge'))
            if tracking and label_b64:
                if order_number:
                    self.label_cache.save(order_number, remote_shipment_id, tracking, label_b64)
                return {
                    'status': 'success',
                    'tracking': tracking,
                    'zpl_b64': label_b64,
                    'cost': cost,
                    'shipment_id': remote_shipment_id,
                }
            return {
                'status': 'error',
                'message': 'Veeqo booked but returned no tracking or label. Try again.',
            }

        fail_entry = failed.get(remote_shipment_id)
        if fail_entry is None and len(failed) == 1:
            fail_entry = next(iter(failed.values()))
        if isinstance(fail_entry, dict):
            return {'status': 'error', 'message': self._error_messages(fail_entry)}

        return {'status': 'error', 'message': 'Veeqo did not book this shipment. Try again.'}

    def void_label(self, remote_shipment_id):
        """Cancel a booked Veeqo shipment (voids the label, requests a refund).

        The path id is the `remote_shipment_id` cached at buy time. Carrier
        policy governs whether the refund is granted; a 204 means the void
        request was accepted.
        """
        if not self.configured:
            return {'status': 'error', 'message': 'Veeqo is not configured.'}
        if not remote_shipment_id:
            return {'status': 'error', 'message': 'No Veeqo shipment id to void.'}
        if self.dry_run:
            return {'status': 'success', 'message': 'Veeqo dry-run shipment voided.'}

        try:
            resp = requests.delete(
                f'{self.base_url}{self.SHIPMENTS_PATH}/{remote_shipment_id}',
                headers=self._headers(), timeout=self.VOID_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("Veeqo void failed: %s", exc)
            return {'status': 'error', 'message': self.friendly_error(exc)}
        if resp.status_code in (200, 202, 204):
            return {'status': 'success', 'message': 'Veeqo label voided.'}
        logger.warning("Veeqo void HTTP %s: %s", resp.status_code, resp.text[:400])
        return {'status': 'error', 'message': self.friendly_error(resp.text)}

    # ---- friendly errors -----------------------------------------------

    @staticmethod
    def _error_messages(obj):
        """Veeqo errors are `{ "error_messages": [str, ...] }`."""
        if isinstance(obj, dict):
            msgs = obj.get('error_messages')
            if isinstance(msgs, list) and msgs:
                return '; '.join(str(m) for m in msgs)[:250]
        return ''

    def friendly_error(self, text_or_exception):
        if isinstance(text_or_exception, str):
            text = text_or_exception
            # Prefer the structured error_messages array when present.
            try:
                import json
                parsed = json.loads(text)
                structured = self._error_messages(parsed)
                if structured:
                    text = structured
            except (ValueError, TypeError):
                pass
        else:
            text = str(text_or_exception)

        lower = text.lower()
        if 'timeout' in lower or 'timed out' in lower:
            return 'Veeqo timed out. Try again or ship via ShipRush.'
        if 'connection' in lower or 'connect' in lower:
            return 'Could not reach Veeqo. Check network or ship via ShipRush.'
        if 'permission' in lower or 'x-api-key' in lower or 'api key' in lower:
            return 'Veeqo rejected the API key or feature access. Check Settings.'
        if len(text) > 250:
            return 'Veeqo error. Try again or ship via ShipRush.'
        return text.strip() or 'Veeqo error. Please try again.'


def _num(raw, cast=float, default=None):
    if raw is None or raw == '':
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default
