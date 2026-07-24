"""Rate shopping inside ship_order(): it may improve a ship, never block one.

Rate shopping now sits in the critical path of every single ship, between
the box scan and the label buy, with an operator standing at the bench.
That placement is only defensible if every failure mode is survivable, so
most of this file is about proving the unhappy paths still ship.
"""

from unittest.mock import MagicMock

from app.services import shipping as shipping_mod
from app.services.shipping import _conflict_rate_summary, _quoted_cost, _quoted_service


QUOTES = [
    {'service_code': 'USPSGNDADV', 'name': 'USPS Ground Advantage',
     'total': 8.71, 'carrier_rate': 8.71, 'transit_days': 2, 'one_rate': False},
    {'service_code': 'FSP', 'name': 'FedEx Ground Economy',
     'total': 10.50, 'carrier_rate': 10.50, 'transit_days': 5, 'one_rate': False},
    {'service_code': '03', 'name': 'UPS Ground',
     'total': 14.36, 'carrier_rate': 14.36, 'transit_days': 2, 'one_rate': False},
]


def _svc_with(rate_shop_return, ordered_code='03'):
    """A ShippingService with just enough wired to exercise _rate_shop_for."""
    svc = shipping_mod.ShippingService.__new__(shipping_mod.ShippingService)
    svc.shiprush = MagicMock()
    svc.shiprush.rate_shop.return_value = rate_shop_return
    svc.shiprush.service_code_for.return_value = ordered_code
    svc.carrier = MagicMock()
    return svc


def _order():
    order = MagicMock()
    order.shipping_address.name = 'Dana Whitfield'
    order.shipping_address.line1 = '1420 N Willow Creek Rd'
    order.shipping_address.line2 = ''
    order.shipping_address.city = 'Bozeman'
    order.shipping_address.state = 'MT'
    order.shipping_address.postal_code = '59718'
    order.shipping_address.country = 'US'
    order.shipping_address.phone = '4065550173'
    order.customer_name = 'Dana Whitfield'
    return order


DIMS = {'l': 12, 'w': 9, 'h': 6}


class TestHappyPath:

    def test_quotes_then_selects(self):
        svc = _svc_with({'status': 'success', 'services': QUOTES})
        out = svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground')
        assert out['chosen']['service_code'] == 'USPSGNDADV'
        assert out['savings'] == 5.65

    def test_baseline_comes_from_the_label_paths_own_resolver(self):
        """rate selection must compare against the service the label path
        would actually buy, not a second guess at it."""
        svc = _svc_with({'status': 'success', 'services': QUOTES})
        svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground')
        svc.shiprush.service_code_for.assert_called_once_with('UPS Ground')

    def test_destination_is_forwarded_to_the_quote(self):
        svc = _svc_with({'status': 'success', 'services': QUOTES})
        svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground')
        addr = svc.shiprush.rate_shop.call_args.args[0]
        assert addr['zip'] == '59718'
        assert addr['state'] == 'MT'
        assert addr['country'] == 'US'


class TestNeverBlocksAShip:
    """Every one of these must return None, not raise and not error."""

    def test_rate_shop_error_returns_none(self):
        svc = _svc_with({'status': 'error', 'message': 'timed out'})
        assert svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground') is None

    def test_rate_shop_exception_returns_none(self):
        svc = _svc_with(None)
        svc.shiprush.rate_shop.side_effect = OSError('connection reset')
        assert svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground') is None

    def test_service_code_lookup_exception_returns_none(self):
        svc = _svc_with({'status': 'success', 'services': QUOTES})
        svc.shiprush.service_code_for.side_effect = KeyError('no such method')
        assert svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground') is None

    def test_empty_service_list_does_not_raise(self):
        svc = _svc_with({'status': 'success', 'services': []})
        out = svc._rate_shop_for(_order(), DIMS, 4.82, '02', 'UPS Ground')
        assert out is None or out.get('chosen') is None


class TestPersistedColumns:
    """quoted_cost must stay None rather than collapse to 0.0 -- a zero
    would read as 'quoted free' in any later margin query."""

    def test_none_quote_yields_none_not_zero(self):
        assert _quoted_cost(None) is None
        assert _quoted_service(None) is None

    def test_unselected_quote_yields_none(self):
        assert _quoted_cost({'chosen': None}) is None
        assert _quoted_service({'chosen': None}) is None

    def test_chosen_quote_yields_price_and_code(self):
        q = {'chosen': {'total': 8.71, 'service_code': 'USPSGNDADV'}}
        assert _quoted_cost(q) == 8.71
        assert _quoted_service(q) == 'USPSGNDADV'


class TestConflictSummary:
    """The prompt gets prices, but not all 18 of them."""

    def test_truncates_the_option_list(self):
        """19 services come back on this account; a pack-station prompt
        cannot be a spreadsheet.

        Distinct services on purpose: with duplicates the cheapest few fill
        the cap and the preserve-chosen-and-ordered rule then re-adds one,
        which measures the wrong thing.
        """
        # Two per carrier across three carriers. A single overall cap let the
        # cheapest carrier fill the list and pushed the other two out, which
        # defeats a side-by-side comparison.
        many = ([dict(QUOTES[0], service_code=f'U{i}0', total=5.0 + i) for i in range(5)]
                + [dict(QUOTES[0], service_code=f'{i}3', total=9.0 + i) for i in range(5)]
                + [dict(QUOTES[0], service_code=f'F{i}0', total=12.0 + i) for i in range(5)])
        quote = {'chosen': many[0], 'ordered': many[1], 'eligible': many}
        opts = _conflict_rate_summary(quote)['options']
        assert len(opts) == 6
        carriers = [o['carrier'] for o in opts]
        assert carriers.count('USPS') == 2
        assert carriers.count('UPS') == 2
        assert carriers.count('FEDEX') == 2

    def test_rejected_services_are_shown_with_their_reason(self):
        """The operator can override the rule, but only to something they
        can see. FedEx Ground Economy losing on transit is exactly the case
        where a human may still want it."""
        quote = {
            'chosen': QUOTES[0], 'ordered': QUOTES[0], 'eligible': [QUOTES[0]],
            'rejected': [(QUOTES[1], '7d vs 5d ordered')],
        }
        opts = _conflict_rate_summary(quote)['options']
        fsp = next(o for o in opts if o['service_code'] == 'FSP')
        assert fsp['note'] == '7d vs 5d ordered'

    def test_accepted_services_carry_no_note(self):
        quote = {'chosen': QUOTES[0], 'ordered': QUOTES[0], 'eligible': QUOTES}
        assert all(o['note'] is None for o in _conflict_rate_summary(quote)['options'])

    def test_chosen_and_ordered_survive_truncation(self):
        """Both are what the prompt is about; neither may be cut by price."""
        cheap = [dict(QUOTES[0], service_code=f'X{i}', total=1.0 + i)
                 for i in range(10)]
        quote = {'chosen': QUOTES[0], 'ordered': QUOTES[2],
                 'eligible': cheap + [QUOTES[0], QUOTES[2]]}
        codes = [o['service_code'] for o in _conflict_rate_summary(quote)['options']]
        assert 'USPSGNDADV' in codes
        assert '03' in codes

    def test_carries_chosen_ordered_and_savings(self):
        quote = {'chosen': QUOTES[0], 'ordered': QUOTES[2], 'savings': 5.65,
                 'reason': 'because', 'eligible': QUOTES}
        out = _conflict_rate_summary(quote)
        assert out['chosen']['service_code'] == 'USPSGNDADV'
        assert out['chosen']['cost'] == 8.71
        assert out['ordered']['service_code'] == '03'
        assert out['savings'] == 5.65

    def test_rows_carry_only_what_the_prompt_needs(self):
        """Rows are flattened, not the raw ShipRush service dict.

        account_id joined the allowlist deliberately: clicking a quoted row
        buys that service on the account that quoted it, rather than
        re-deriving the account from the ship method and risking the
        service/account mismatch behind stikman28/dockd#6.
        """
        out = _conflict_rate_summary({'chosen': QUOTES[0], 'eligible': QUOTES})
        assert set(out['chosen']) == {
            'name', 'service_code', 'cost', 'transit_days', 'one_rate',
            'account_id', 'carrier', 'requires_fedex_box'}

    def test_internal_quote_fields_never_reach_the_page(self):
        """Everything else ShipRush returns stays server-side."""
        rich = dict(QUOTES[0], quote_id='Q-123', markup=1.5, carrier_rate=8.0,
                    packaging_type='02', is_estimated=True)
        out = _conflict_rate_summary({'chosen': rich, 'eligible': [rich]})
        for leaked in ('quote_id', 'markup', 'carrier_rate', 'packaging_type',
                       'is_estimated'):
            assert leaked not in out['chosen'], f'{leaked} leaked to the browser'

    def test_none_quote_summarises_to_none(self):
        assert _conflict_rate_summary(None) is None
