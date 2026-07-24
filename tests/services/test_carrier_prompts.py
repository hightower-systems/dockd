"""Amazon and high-value carrier prompts, relocated to gate 3 (v2).

Both used to be client-side conditionals in fetchOrder(), firing at order
load. They moved server-side into _carrier_prompt_for() for two reasons:

  1. Neither could be priced where they were. No box had been scanned, so
     no weight and no dims existed, so no quote was possible. Operators
     were choosing between carriers -- sometimes a $12 swing -- with no
     number on screen.
  2. A stale browser could skip either prompt entirely by POSTing straight
     to /ship_order. Server-side, the rule is enforced rather than merely
     displayed.

The behaviour itself is deliberately unchanged: same method matching, same
threshold semantics, so the move is observable only in *when* the operator
is asked, not *whether*.
"""

from unittest.mock import MagicMock

from app.services import shipping as shipping_mod


def _svc(settings_map):
    svc = shipping_mod.ShippingService.__new__(shipping_mod.ShippingService)
    settings = MagicMock()
    settings.get.side_effect = lambda key, *a: settings_map.get(key)
    svc.settings = settings
    return svc


def _order(total=100.0, paid=9.99):
    o = MagicMock()
    o.order_total = total
    o.customer_shipping_paid = paid
    return o


AMAZON = {'amazon_methods': ['std us dom_2', 'std us dom'], 'high_value_threshold': 200}


class TestAmazon:

    def test_configured_amazon_method_prompts(self):
        out = _svc(AMAZON)._carrier_prompt_for(_order(), 'Std US Dom_2')
        assert out['status'] == 'amazon_carrier'

    def test_matching_is_case_insensitive(self):
        out = _svc(AMAZON)._carrier_prompt_for(_order(), 'STD US DOM_2')
        assert out['status'] == 'amazon_carrier'

    def test_non_amazon_method_does_not_prompt(self):
        assert _svc(AMAZON)._carrier_prompt_for(_order(), 'USPS Ground Advantage') is None

    def test_amazon_beats_high_value_when_both_would_fire(self):
        """One prompt per ship. Amazon wins because it is a hard question --
        no carrier was named at all -- where high value is an upsell."""
        cfg = {'amazon_methods': ['usps ground advantage'], 'high_value_threshold': 50}
        out = _svc(cfg)._carrier_prompt_for(_order(total=500), 'USPS Ground Advantage')
        assert out['status'] == 'amazon_carrier'


class TestHighValue:

    def test_prompts_over_threshold_on_usps(self):
        out = _svc(AMAZON)._carrier_prompt_for(_order(total=486.20), 'USPS Ground Advantage')
        assert out['status'] == 'high_value'
        assert '486.20' in out['reason']

    def test_silent_under_threshold(self):
        assert _svc(AMAZON)._carrier_prompt_for(_order(total=50), 'USPS Ground Advantage') is None

    def test_threshold_zero_disables_it(self):
        """Matches the existing convention: an unconfigured install prompts
        for nothing rather than guessing a dollar figure."""
        cfg = {'amazon_methods': [], 'high_value_threshold': 0}
        assert _svc(cfg)._carrier_prompt_for(_order(total=9999), 'USPS Priority') is None

    def test_does_not_prompt_when_already_premium(self):
        for method in ('UPS Ground', 'FedEx 2Day', 'FedEx Ground'):
            assert _svc(AMAZON)._carrier_prompt_for(_order(total=999), method) is None

    def test_recognises_usps_service_names_without_the_usps_token(self):
        """'Priority Mail' and 'Ground Advantage' are USPS services whose
        names never say USPS. Missing these was a live bug class before."""
        for method in ('Ground Advantage', 'Priority Mail', 'First Class Package'):
            out = _svc(AMAZON)._carrier_prompt_for(_order(total=999), method)
            assert out is not None, method
            assert out['status'] == 'high_value'

    def test_exactly_at_threshold_prompts(self):
        out = _svc(AMAZON)._carrier_prompt_for(_order(total=200.0), 'USPS Priority')
        assert out['status'] == 'high_value'


class TestDegradesSafely:

    def test_no_settings_store_means_no_prompt(self):
        svc = shipping_mod.ShippingService.__new__(shipping_mod.ShippingService)
        svc.settings = None
        assert svc._carrier_prompt_for(_order(), 'USPS Priority') is None

    def test_blank_ship_method_does_not_prompt(self):
        assert _svc(AMAZON)._carrier_prompt_for(_order(total=999), '') is None
        assert _svc(AMAZON)._carrier_prompt_for(_order(total=999), None) is None

    def test_unparseable_threshold_is_treated_as_disabled(self):
        cfg = {'amazon_methods': [], 'high_value_threshold': 'not a number'}
        assert _svc(cfg)._carrier_prompt_for(_order(total=999), 'USPS Priority') is None

    def test_missing_amazon_list_does_not_raise(self):
        cfg = {'high_value_threshold': 200}
        assert _svc(cfg)._carrier_prompt_for(_order(total=10), 'Whatever') is None

    def test_prompt_carries_the_money_the_modal_shows(self):
        out = _svc(AMAZON)._carrier_prompt_for(_order(total=486.20, paid=12.95),
                                               'USPS Ground Advantage')
        assert out['order_total'] == 486.20
        assert out['ca_shipping_paid'] == 12.95
