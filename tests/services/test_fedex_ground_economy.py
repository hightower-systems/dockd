"""FedEx Ground Economy (FSP) routing.

Grounded in the 2026-07-20 backtest, which re-priced 543 real UPS shipments
through /rateshopping: Ground Economy came in ~32% under UPS Ground on
like-for-like parcels, worth roughly $25k/yr, but only below 20 billable
pounds. It is DIM-priced, so large light boxes lose badly -- a 1lb 36x16x5
rod tube quoted $71.25 on UPS against $112.49 on GE.

The routing half of this is a safety fix, not a feature. Before it, a ship
method named "FedEx Ground Economy" matched no FedEx branch, fell through to
FEDEX_GROUND, and with that slot unconfigured landed on _RESOLVE_FALLBACK --
which is UPS Ground on the UPS account. The order said FedEx and the label
said UPS, with nothing on screen to say so.
"""

from unittest.mock import MagicMock

import pytest

from app.services.rate_select import select_rate
from app.services.shiprush import ShipRushClient


FULL_CATALOG = {
    'UPS_GROUND':          {'carrier_id': '1', 'account_key': 'UPS', 'service_code': '03'},
    'FEDEX_GROUND':        {'carrier_id': '1', 'account_key': 'FEDEX', 'service_code': 'F92'},
    'FEDEX_GROUND_ECONOMY': {'carrier_id': '1', 'account_key': 'FEDEX', 'service_code': 'FSP'},
    'FEDEX_2DAY':          {'carrier_id': '1', 'account_key': 'FEDEX', 'service_code': 'F03'},
    'USPS_GROUND_ADV':     {'carrier_id': '2', 'account_key': 'USPS', 'service_code': 'USPSGNDADV'},
}


def _client(catalog):
    settings = MagicMock()
    settings.get.side_effect = lambda key, *a: (
        catalog if key == 'shiprush_services' else ({} if key.endswith('s') else {}))
    return ShipRushClient(settings, MagicMock())


class TestRouting:

    @pytest.mark.parametrize('method', [
        'FedEx Ground Economy',
        'fedex ground economy',
        'FedEx SmartPost',
        'FedEx Smart Post',
    ])
    def test_economy_methods_resolve_to_fsp(self, method):
        assert _client(FULL_CATALOG).service_code_for(method) == 'FSP'

    def test_economy_is_matched_before_plain_ground(self):
        """'FedEx Ground Economy' contains 'ground'. If the plain-ground
        branch wins, GE orders quietly ship as FedEx Ground."""
        c = _client(FULL_CATALOG)
        assert c.service_code_for('FedEx Ground Economy') == 'FSP'
        assert c.service_code_for('FedEx Ground') == 'F92'

    def test_economy_stays_on_the_fedex_account(self):
        carrier_id, account, service, _ = _client(FULL_CATALOG)._resolve_carrier(
            'fedex ground economy', None)
        assert account == 'FEDEX'
        assert service == 'FSP'

    def test_other_fedex_services_still_route_correctly(self):
        c = _client(FULL_CATALOG)
        assert c.service_code_for('FedEx 2Day') == 'F03'
        assert c.service_code_for('FedEx Ground') == 'F92'


class TestUnconfiguredSlotIsLoud:
    """The pre-fix behaviour was silent. It must never be silent again.

    Asserted by substituting the module logger rather than by capturing log
    output: pytest's capture plumbing does not reliably see this record once
    create_app() has configured the 'dockd' logger tree in the session
    fixture, and a test for "did we warn" should not itself depend on
    handler wiring.
    """

    @staticmethod
    def _errors_while(monkeypatch, fn):
        from app.services import shiprush as mod
        calls = []

        class StubLogger:
            def error(self, msg, *args, **kw):
                calls.append(msg % args if args else msg)

            def __getattr__(self, _):
                return lambda *a, **k: None

        monkeypatch.setattr(mod, 'logger', StubLogger())
        fn()
        return calls

    def test_unconfigured_economy_falls_back_and_logs_an_error(self, monkeypatch):
        bare = {'UPS_GROUND': FULL_CATALOG['UPS_GROUND']}
        client = _client(bare)
        out = {}

        msgs = self._errors_while(
            monkeypatch,
            lambda: out.setdefault(
                'r', client._resolve_carrier('fedex ground economy', None)))

        # It still degrades to the fallback rather than dying mid-pack...
        assert out['r'][1] == 'UPS'
        # ...but it says so, loudly, naming the mis-ship.
        assert any('FedEx' in m and 'UPS' in m for m in msgs), \
            f'no error logged for a FedEx->UPS resolve; got {msgs}'

    def test_the_error_names_the_offending_method(self, monkeypatch):
        bare = {'UPS_GROUND': FULL_CATALOG['UPS_GROUND']}
        client = _client(bare)
        msgs = self._errors_while(
            monkeypatch,
            lambda: client._resolve_carrier('FedEx Ground Economy', None))
        assert any('fedex ground economy' in m.lower() for m in msgs), \
            f'the error should name the method that caused it; got {msgs}'

    def test_configured_fedex_logs_nothing(self, monkeypatch):
        client = _client(FULL_CATALOG)
        msgs = self._errors_while(
            monkeypatch,
            lambda: client._resolve_carrier('fedex ground economy', None))
        assert msgs == []


class TestDimCeiling:
    """GE below 20 billable lb, UPS above it."""

    def _quotes(self):
        return [
            {'service_code': 'FSP', 'name': 'FedEx Ground Economy',
             'total': 8.00, 'transit_days': 5},
            {'service_code': '03', 'name': 'UPS Ground',
             'total': 14.36, 'transit_days': 5},
        ]

    def test_economy_wins_under_the_ceiling(self):
        out = select_rate(self._quotes(), ordered_code='03', billable_lb=6.0)
        assert out['chosen']['service_code'] == 'FSP'

    def test_economy_is_excluded_above_the_ceiling(self):
        """The rod-tube case: cheap on paper, far dearer in reality."""
        out = select_rate(self._quotes(), ordered_code='03', billable_lb=26.0)
        assert out['chosen']['service_code'] == '03'
        assert any(s['service_code'] == 'FSP' and 'dim ceiling' in why
                   for s, why in out['rejected'])

    def test_exactly_at_the_ceiling_still_allows_it(self):
        out = select_rate(self._quotes(), ordered_code='03', billable_lb=20.0)
        assert out['chosen']['service_code'] == 'FSP'

    def test_no_billable_weight_does_not_exclude_it(self):
        """Missing weight must not silently drop a service."""
        out = select_rate(self._quotes(), ordered_code='03', billable_lb=None)
        assert out['chosen']['service_code'] == 'FSP'

    def test_ceiling_applies_only_to_dim_capped_services(self):
        out = select_rate(self._quotes(), ordered_code='03', billable_lb=40.0)
        assert out['chosen']['service_code'] == '03'


class TestClickedServiceOverride:
    """A clicked rate row buys that exact service on the account that quoted
    it, rather than re-deriving either from the ship method."""

    def test_dict_override_wins_over_slot_resolution(self):
        carrier_id, account, service, one_rate = _client(FULL_CATALOG)._resolve_carrier(
            'usps ground advantage',
            {'service_code': 'FSP', 'account_key': 'FEDEX', 'carrier_id': '1'})
        assert service == 'FSP'
        assert account == 'FEDEX'

    def test_override_carries_the_one_rate_flag(self):
        *_, one_rate = _client(FULL_CATALOG)._resolve_carrier(
            'ups ground', {'service_code': 'F03', 'one_rate': True})
        assert one_rate is True

    def test_string_overrides_still_work(self):
        c = _client(FULL_CATALOG)
        assert c._resolve_carrier('anything', 'UPS')[2] == '03'
        assert c._resolve_carrier('anything', 'USPS')[2] == 'USPSGNDADV'
