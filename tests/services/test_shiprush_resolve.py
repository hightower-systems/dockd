"""Regression tests for ShipRushClient._resolve_carrier.

v0.6.0: the prior `return row(slot)` pattern returned `None` when the
slot was absent from `shiprush_services`, and the caller unpacked the
result as a 4-tuple in the generate_label flow. With the open-source
neutral defaults (`shiprush_services={}` until an admin configures
the catalog), every ship attempt raised
`TypeError: cannot unpack non-iterable NoneType object` at the
ShipRush XML build step.

The fix: `_resolve_or_fallback(*slots)` walks the candidates and
falls back to a hardcoded 4-tuple (`'1', 'UPS', '03', False`) so the
unpack always succeeds. ShipRush may still reject the request later
(unknown account, bad service code) but with a structured ShipRush
error -- not a Python 500.
"""

from unittest.mock import MagicMock


def _make_client(services=None, accounts=None):
    """Build a ShipRushClient with a fake settings store."""
    from app.services.shiprush import ShipRushClient

    class _FakeSettings:
        def __init__(self, payload):
            self._data = payload

        def get(self, key, default=None):
            return self._data.get(key, default)

    settings = _FakeSettings({
        'shiprush_services': services or {},
        'shiprush_accounts': accounts or {},
        'shipper_origin': {},
        'fallback_customer_phone': '',
    })
    return ShipRushClient(settings, MagicMock())


class TestEmptyServices:

    def test_empty_services_returns_fallback_tuple(self):
        """Every ship-method branch lands on the fallback when
        shiprush_services is empty -- never None."""
        c = _make_client(services={})
        for method in (
            'USPS Ground Advantage',
            'UPS - Ground',
            'FedEx One Rate 2 Day',
            'FedEx Ground',
            'FedEx Overnight',
            'USPS Priority',
            'USPS Media Mail',
            'USPS First Class',
            'USPS Parcel',
            'UPS Next Day',
            'UPS 2nd Day',
            'UPS 3 Day',
            'Some Unknown Method',
            '',
        ):
            result = c._resolve_carrier(method, None)
            assert result is not None, f"method={method!r} returned None"
            assert isinstance(result, tuple), f"method={method!r} returned non-tuple"
            assert len(result) == 4, f"method={method!r} returned wrong arity"
            carrier_id, account_key, service_code, is_one_rate = result
            assert isinstance(carrier_id, str)
            assert isinstance(account_key, str)
            assert isinstance(service_code, str)
            assert isinstance(is_one_rate, bool)

    def test_empty_services_with_overrides_returns_fallback(self):
        c = _make_client(services={})
        for override in ('UPS', 'USPS', 'FEDEX_ONE_RATE_2DAY'):
            result = c._resolve_carrier('USPS Ground Advantage', override)
            assert result == c._RESOLVE_FALLBACK


class TestPopulatedServices:

    def test_usps_ground_adv_matches(self):
        c = _make_client(services={
            'USPS_GROUND_ADV': {
                'carrier_id': '18', 'account_key': 'USPS_EASYPOST',
                'service_code': 'USPSGNDADV', 'is_one_rate': False,
            },
        })
        carrier_id, account_key, service_code, is_one_rate = c._resolve_carrier(
            'USPS Ground Advantage', None,
        )
        assert carrier_id == '18'
        assert account_key == 'USPS_EASYPOST'
        assert service_code == 'USPSGNDADV'
        assert is_one_rate is False

    def test_override_ups_matches_ups_ground_slot(self):
        c = _make_client(services={
            'UPS_GROUND': {
                'carrier_id': '1', 'account_key': 'UPS',
                'service_code': '03', 'is_one_rate': False,
            },
        })
        assert c._resolve_carrier(None, 'UPS') == ('1', 'UPS', '03', False)

    def test_fedex_2day_one_rate(self):
        c = _make_client(services={
            'FEDEX_2DAY': {
                'carrier_id': '1', 'account_key': 'FEDEX',
                'service_code': 'F03', 'is_one_rate': True,
            },
        })
        carrier_id, account_key, service_code, is_one_rate = c._resolve_carrier(
            'FedEx 2 Day', None,
        )
        assert (carrier_id, account_key, service_code, is_one_rate) == \
            ('1', 'FEDEX', 'F03', True)

    def test_secondary_fallback_chain(self):
        """USPS_PRIORITY is the preferred slot for "Priority Mail" but
        if it's absent the resolver should try USPS_GROUND_ADV next,
        then the hardcoded fallback."""
        # Only USPS_GROUND_ADV is configured.
        c = _make_client(services={
            'USPS_GROUND_ADV': {
                'carrier_id': '18', 'account_key': 'USPS_EASYPOST',
                'service_code': 'USPSGNDADV', 'is_one_rate': False,
            },
        })
        result = c._resolve_carrier('USPS Priority Mail', None)
        # USPS_PRIORITY slot absent; falls through to USPS_GROUND_ADV.
        assert result == ('18', 'USPS_EASYPOST', 'USPSGNDADV', False)
