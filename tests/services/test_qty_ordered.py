"""Sentry contract: items[].qty_ordered.

Sentry's GET /api/v1/dockd/orders/<so> response now carries both:
  - qty: units physically in the tote awaiting scan-verify
    (sales_order_lines.quantity_picked). Unchanged from prior contract.
  - qty_ordered: units the customer originally ordered
    (sales_order_lines.quantity_ordered). qty < qty_ordered means the
    line was short-picked or partial-fulfilled; the difference will
    not ship on this SO.

Covers:
  * OrderItem.from_dict reads qty_ordered when present.
  * OrderItem.from_dict defaults qty_ordered=None when missing, so new
    dockd against pre-contract Sentry still works (qty-only display).
  * `_coerce_int` defensive parsing: floors to default on None, on
    non-numeric strings, and on garbage; floor-truncates decimal
    strings and JSON numbers.
  * Response shaper passes qty_ordered through 1:1 alongside qty.
"""

from app.services.backend import OrderItem
from app.services.backend import _coerce_int
from app.services.shipping import _order_to_load_dict
from app.services.backend import OrderData, ShippingAddress


# ---------------------------------------------------------------------------
# _coerce_int
# ---------------------------------------------------------------------------


class TestCoerceInt:

    def test_int_passthrough(self):
        assert _coerce_int(3) == 3

    def test_zero_is_zero_not_default(self):
        # 0 is a legitimate value (e.g. fully-shipped backorder line);
        # don't fall through to the default just because it's falsy.
        assert _coerce_int(0, default=5) == 0

    def test_negative_passthrough(self):
        # Negative qty would be a Sentry bug, but we parse what we're
        # given and let the UI / downstream catch the semantic issue.
        assert _coerce_int(-2) == -2

    def test_numeric_string(self):
        assert _coerce_int("4") == 4

    def test_decimal_string_floors(self):
        # Sentry contract says integer-only, but if a malformed payload
        # sends "2.5" we floor rather than raise.
        assert _coerce_int("2.5") == 2

    def test_json_float_floors(self):
        assert _coerce_int(2.999) == 2

    def test_none_returns_default(self):
        assert _coerce_int(None) == 0
        assert _coerce_int(None, default=7) == 7

    def test_garbage_string_returns_default(self):
        assert _coerce_int("abc") == 0
        assert _coerce_int("abc", default=1) == 1

    def test_empty_string_returns_default(self):
        assert _coerce_int("") == 0


# ---------------------------------------------------------------------------
# OrderItem.from_dict
# ---------------------------------------------------------------------------


class TestOrderItemQtyOrdered:

    def test_qty_ordered_present(self):
        item = OrderItem.from_dict({
            'external_id': '1',
            'sku': 'WIDGET',
            'display_name': 'Widget',
            'upc': '012345678905',
            'qty': 2,
            'qty_ordered': 3,
        })
        assert item.qty == 2
        assert item.qty_ordered == 3

    def test_qty_ordered_missing_defaults_none(self):
        # Backward compat: new dockd against old Sentry response. No
        # crash; qty_ordered just isn't present so the UI falls back
        # to the qty-only badge display.
        item = OrderItem.from_dict({
            'external_id': '1',
            'sku': 'WIDGET',
            'display_name': 'Widget',
            'upc': '012345678905',
            'qty': 2,
        })
        assert item.qty == 2
        assert item.qty_ordered is None

    def test_qty_ordered_equal_to_qty_full_pick(self):
        # The 99% case: nothing was short-picked.
        item = OrderItem.from_dict({
            'external_id': '1',
            'sku': 'WIDGET',
            'display_name': 'Widget',
            'upc': '012345678905',
            'qty': 3,
            'qty_ordered': 3,
        })
        assert item.qty == item.qty_ordered == 3

    def test_qty_ordered_short_pick(self):
        # Intentional short-pick: 2 of 3 ordered will ship on this SO.
        item = OrderItem.from_dict({
            'external_id': '1',
            'sku': 'WIDGET',
            'display_name': 'Widget',
            'upc': '012345678905',
            'qty': 2,
            'qty_ordered': 3,
        })
        assert item.qty < item.qty_ordered

    def test_qty_garbage_does_not_500(self):
        # Defensive parsing: a malformed Sentry payload returns a
        # usable OrderItem with qty=0 instead of raising at load time.
        item = OrderItem.from_dict({
            'external_id': '1',
            'sku': 'WIDGET',
            'display_name': 'Widget',
            'upc': '012345678905',
            'qty': 'banana',
            'qty_ordered': None,
        })
        assert item.qty == 0
        assert item.qty_ordered is None

    def test_qty_ordered_zero(self):
        # Legitimate (if unusual) zero value: a line was wholly
        # backordered with nothing shipping. Don't coerce away.
        item = OrderItem.from_dict({
            'external_id': '1',
            'sku': 'WIDGET',
            'display_name': 'Widget',
            'upc': '012345678905',
            'qty': 0,
            'qty_ordered': 3,
        })
        assert item.qty == 0
        assert item.qty_ordered == 3


# ---------------------------------------------------------------------------
# Response shaper
# ---------------------------------------------------------------------------


def _make_order(items):
    return OrderData(
        so_number='SO-1',
        external_id='1',
        status='READY_TO_SHIP',
        warehouse_id=1,
        shippable=True,
        shippable_from_statuses=['READY_TO_SHIP'],
        items=items,
        shipping_address=ShippingAddress(country='US'),
    )


class TestOrderToLoadDictPassesQtyOrdered:

    def test_qty_ordered_passed_through_when_set(self):
        order = _make_order([
            OrderItem(external_id='1', sku='A', display_name='A',
                      upc=None, qty=1, qty_ordered=2),
        ])
        payload = _order_to_load_dict(order)
        assert payload['items'][0]['qty'] == 1
        assert payload['items'][0]['qty_ordered'] == 2

    def test_qty_ordered_none_passes_through_as_none(self):
        order = _make_order([
            OrderItem(external_id='1', sku='A', display_name='A',
                      upc=None, qty=1, qty_ordered=None),
        ])
        payload = _order_to_load_dict(order)
        assert payload['items'][0]['qty'] == 1
        assert payload['items'][0]['qty_ordered'] is None
