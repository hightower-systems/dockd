"""International-shipping support (v0.7.0).

Covers:
  * `CustomsData` round-trip from Sentry-shaped dicts (and defensive
    normalization: uppercase ISO 3166 alpha-2, negative-value rejection).
  * `OrderItem` carries customs; `OrderData` carries `currency` and
    `duties_paid_by`.
  * `_normalize_country` / `_is_international` / `_build_customs_items`
    helpers in `app.services.shipping`.
  * `_build_shiprush_payload` passes country + customs + currency
    through to the ShipRush adapter dict.
  * `ShipRushClient.generate_label` emits `<Country>` from the address
    (NOT hardcoded US) and emits the `<Commodities>` block only for
    non-US destinations.
  * `_carrier_from_tracking` recognizes international tracking-number
    prefixes (DHL, Royal Mail / Canada Post / USPS S10 alphanumeric).
  * `SettingsStore.is_country_banned` enforces the banned-destination
    list with case/whitespace normalization.
  * `SettingsStore.public_subset` does NOT leak `shipper_tax_ids`.
"""

import os
import re
import tempfile
from unittest.mock import MagicMock

import pytest

from app.services.backend import (
    CustomsData,
    OrderData,
    OrderItem,
    ShippingAddress,
)
from app.services.default_settings import DEFAULT_SETTINGS
from app.services.label_cache import LabelCache
from app.services.settings import SettingsStore
from app.services.shipping import (
    _build_customs_items,
    _build_shiprush_payload,
    _carrier_from_tracking,
    _is_international,
    _normalize_country,
)
from app.services.shiprush import ShipRushClient


# ---------------------------------------------------------------------------
# CustomsData + OrderItem extension
# ---------------------------------------------------------------------------


class TestCustomsData:

    def test_round_trip_full_data(self):
        c = CustomsData.from_dict({
            'description': 'Fly reel',
            'hs_code': '9507.30.40',
            'country_of_origin': 'US',
            'unit_weight_oz': 5.4,
            'unit_value': 425.0,
        })
        assert c.description == 'Fly reel'
        assert c.hs_code == '9507.30.40'
        assert c.country_of_origin == 'US'
        assert c.unit_weight_oz == pytest.approx(5.4)
        assert c.unit_value == pytest.approx(425.0)

    def test_empty_dict_returns_none(self):
        assert CustomsData.from_dict(None) is None
        assert CustomsData.from_dict({}) is None

    def test_country_normalized_to_uppercase_alpha2(self):
        # "usa" -> "US"; "ca" -> "CA"; "  jp  " -> "JP".
        assert CustomsData.from_dict({'country_of_origin': 'usa'}).country_of_origin == 'US'
        assert CustomsData.from_dict({'country_of_origin': 'ca'}).country_of_origin == 'CA'
        assert CustomsData.from_dict({'country_of_origin': '  jp  '}).country_of_origin == 'JP'

    def test_negative_numbers_dropped(self):
        c = CustomsData.from_dict({
            'unit_weight_oz': -3.0,
            'unit_value': -10.0,
        })
        assert c.unit_weight_oz is None
        assert c.unit_value is None

    def test_garbage_numbers_dropped_without_raising(self):
        c = CustomsData.from_dict({
            'unit_weight_oz': 'not a number',
            'unit_value': None,
        })
        assert c.unit_weight_oz is None
        assert c.unit_value is None


class TestOrderItemWithCustoms:

    def test_item_without_customs_round_trips_unchanged(self):
        item = OrderItem.from_dict({
            'external_id': '123',
            'sku': 'RBT-2500',
            'display_name': 'Ross Reel',
            'upc': '012345678905',
            'qty': 1,
        })
        assert item.customs is None

    def test_item_with_customs_round_trips(self):
        item = OrderItem.from_dict({
            'external_id': '123',
            'sku': 'RBT-2500',
            'display_name': 'Ross Reel',
            'upc': '012345678905',
            'qty': 2,
            'customs': {
                'description': 'Fishing reel',
                'hs_code': '9507.30.40',
                'country_of_origin': 'us',
                'unit_weight_oz': 5.4,
                'unit_value': 425.0,
            },
        })
        assert item.customs is not None
        assert item.customs.country_of_origin == 'US'


class TestOrderDataIntlFields:

    def test_currency_defaults_to_usd(self):
        order = OrderData.from_dict({
            'so_number': 'SO-1', 'external_id': '', 'status': 'PACKED',
            'warehouse_id': 1, 'shippable': True,
            'shippable_from_statuses': ['PACKED'], 'items': [],
            'shipping_address': {},
        })
        assert order.currency == 'USD'
        assert order.duties_paid_by is None

    def test_currency_normalized_uppercase(self):
        order = OrderData.from_dict({
            'so_number': 'SO-1', 'external_id': '', 'status': 'PACKED',
            'warehouse_id': 1, 'shippable': True,
            'shippable_from_statuses': ['PACKED'], 'items': [],
            'shipping_address': {},
            'currency': 'cad',
            'duties_paid_by': 'SENDER',
        })
        assert order.currency == 'CAD'
        assert order.duties_paid_by == 'sender'


# ---------------------------------------------------------------------------
# Shipping-service helpers
# ---------------------------------------------------------------------------


class TestCountryNormalization:

    def test_normalize_blank_to_us(self):
        assert _normalize_country(None) == 'US'
        assert _normalize_country('') == 'US'

    def test_normalize_strips_and_uppercases(self):
        assert _normalize_country(' ca ') == 'CA'
        assert _normalize_country('GB') == 'GB'

    def test_normalize_truncates_to_two_chars(self):
        assert _normalize_country('USA') == 'US'


def _make_order(country='US', items=None, currency='USD', duties=None):
    return OrderData(
        so_number='SO-1', external_id='ext', status='PACKED',
        warehouse_id=1, shippable=True, shippable_from_statuses=['PACKED'],
        items=items or [], shipping_address=ShippingAddress(country=country),
        currency=currency, duties_paid_by=duties,
    )


class TestIsInternational:

    def test_us_is_domestic(self):
        assert _is_international(_make_order(country='US')) is False

    def test_blank_country_is_domestic(self):
        assert _is_international(_make_order(country=None)) is False

    def test_canada_is_international(self):
        assert _is_international(_make_order(country='CA')) is True


class TestBuildCustomsItems:

    def test_skips_items_without_customs(self):
        items = [OrderItem(external_id='1', sku='A', display_name='Item A',
                           upc=None, qty=1, customs=None)]
        assert _build_customs_items(_make_order(items=items)) == []

    def test_projects_items_with_customs(self):
        c = CustomsData(description='Fly line', hs_code='9507.90',
                        country_of_origin='US', unit_weight_oz=2.0,
                        unit_value=99.99)
        items = [OrderItem(external_id='1', sku='FL-50', display_name='Fly Line',
                           upc=None, qty=3, customs=c)]
        result = _build_customs_items(_make_order(items=items))
        assert len(result) == 1
        assert result[0]['hs_code'] == '9507.90'
        assert result[0]['country_of_origin'] == 'US'
        assert result[0]['qty'] == 3
        assert result[0]['unit_value'] == 99.99


# ---------------------------------------------------------------------------
# _build_shiprush_payload propagation
# ---------------------------------------------------------------------------


class TestPayloadCountryPropagation:

    def test_domestic_payload_country_us(self):
        payload = _build_shiprush_payload(_make_order(country='US'))
        assert payload['shippingAddress']['country'] == 'US'
        assert payload['customs_items'] == []
        assert payload['currency'] == 'USD'
        assert payload['duties_paid_by'] is None

    def test_international_payload_country_and_currency(self):
        payload = _build_shiprush_payload(_make_order(
            country='CA', currency='CAD', duties='sender',
        ))
        assert payload['shippingAddress']['country'] == 'CA'
        assert payload['currency'] == 'CAD'
        assert payload['duties_paid_by'] == 'sender'

    def test_payload_includes_customs_items(self):
        c = CustomsData(description='Fly reel', hs_code='9507.30',
                        country_of_origin='US', unit_weight_oz=4.0,
                        unit_value=199.0)
        items = [OrderItem(external_id='1', sku='RR', display_name='Reel',
                           upc=None, qty=2, customs=c)]
        payload = _build_shiprush_payload(_make_order(country='GB', items=items))
        assert len(payload['customs_items']) == 1
        assert payload['customs_items'][0]['hs_code'] == '9507.30'


# ---------------------------------------------------------------------------
# ShipRush XML emission
# ---------------------------------------------------------------------------


@pytest.fixture
def shiprush_client():
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        'shiprush_accounts': {'UPS': 'guid-ups', 'USPS_EASYPOST': 'guid-usps'},
        'shiprush_services': {
            'UPS_GROUND': {'carrier_id': '1', 'account_key': 'UPS',
                           'service_code': '03', 'is_one_rate': False},
        },
        'shipper_origin': {'company': 'Origin Co', 'address1': '1 Main',
                           'city': 'Denver', 'state': 'CO', 'postal_code': '80202',
                           'country': 'US', 'phone': '303-555-0100'},
        'fallback_customer_phone': '',
    }.get(key, default if default is not None else {})
    label_cache = MagicMock()
    return ShipRushClient(settings, label_cache)


def _ship_payload(country='US', customs_items=None, currency='USD', duties=None):
    return {
        'tranId': 'SO-1',
        'shippingAddress': {
            'addressee': 'Test Customer', 'addr1': '1 Foreign St',
            'city': 'Toronto', 'state': 'ON', 'zip': 'M5V 2T6',
            'country': country, 'addrPhone': '4165550100',
        },
        'entity': {'refName': 'Test Customer'},
        'shipMethod': {'refName': 'UPS Worldwide Saver'},
        'customs_items': customs_items or [],
        'currency': currency,
        'duties_paid_by': duties,
        'item': {'items': []}, 'package': {'items': []},
    }


class TestShipRushXmlCountryEmission:

    def test_domestic_emits_us_country(self, shiprush_client):
        # Stub the HTTP call so we can inspect the payload mid-flight.
        captured = {}

        def fake_post(url, data, headers, timeout):
            captured['data'] = data
            return MagicMock(text='<IsSuccess>false</IsSuccess><Text>stub</Text>')
        shiprush_client.endpoint  # touch property
        import requests as r
        orig = r.post
        try:
            r.post = fake_post
            shiprush_client.generate_label(
                _ship_payload(country='US'),
                {'l': 10, 'w': 6, 'h': 4}, 1.0,
            )
        finally:
            r.post = orig
        body = captured['data']
        assert '<Country>US</Country>' in body
        assert '<Commodities>' not in body
        assert '<CustomsValue>' not in body

    def test_international_emits_destination_country(self, shiprush_client):
        captured = {}

        def fake_post(url, data, headers, timeout):
            captured['data'] = data
            return MagicMock(text='<IsSuccess>false</IsSuccess><Text>stub</Text>')
        import requests as r
        orig = r.post
        try:
            r.post = fake_post
            shiprush_client.generate_label(
                _ship_payload(country='CA',
                              customs_items=[{
                                  'description': 'Reel', 'hs_code': '9507.30',
                                  'country_of_origin': 'US', 'qty': 1,
                                  'unit_weight_oz': 5.4, 'unit_value': 425.0,
                              }],
                              currency='CAD', duties='sender'),
                {'l': 10, 'w': 6, 'h': 4}, 1.0,
            )
        finally:
            r.post = orig
        body = captured['data']
        assert '<Country>CA</Country>' in body
        assert '<Commodities>' in body
        assert '<HarmonizedCode>9507.30</HarmonizedCode>' in body
        assert '<CustomsValue><Amount>425.00</Amount>' in body
        assert '<Currency>CAD</Currency>' in body
        assert '<IncotermsCode>DDP</IncotermsCode>' in body
        assert '<ContentType>Merchandise</ContentType>' in body


class TestAdultSignatureDcisEmission:
    """v0.7.0: per-ship adult-signature toggle emits a <DCISType>
    element inside the package block. ShipRush XSD TDCIS enum:
    'ADS' for UPS / USPS, 'F4' for FedEx. Element is omitted when
    the toggle is off so legacy domestic ships remain unchanged.
    """

    def _capture_xml(self, shiprush_client, payload, adult_signature):
        captured = {}

        def fake_post(url, data, headers, timeout):
            captured['data'] = data
            return MagicMock(text='<IsSuccess>false</IsSuccess><Text>stub</Text>')
        import requests as r
        orig = r.post
        try:
            r.post = fake_post
            shiprush_client.generate_label(
                payload, {'l': 10, 'w': 6, 'h': 4}, 1.0,
                adult_signature=adult_signature,
            )
        finally:
            r.post = orig
        return captured['data']

    def _payload_for_method(self, method):
        return {
            'tranId': 'SO-1',
            'shippingAddress': {
                'addressee': 'C', 'addr1': '1 Main', 'city': 'Denver',
                'state': 'CO', 'zip': '80202', 'country': 'US',
                'addrPhone': '3035550100',
            },
            'entity': {'refName': 'C'},
            'shipMethod': {'refName': method},
            'customs_items': [], 'currency': 'USD', 'duties_paid_by': None,
            'item': {'items': []}, 'package': {'items': []},
        }

    def test_off_emits_no_dcis_tag(self, shiprush_client):
        body = self._capture_xml(
            shiprush_client, self._payload_for_method('UPS Ground'),
            adult_signature=False,
        )
        assert '<DCISType>' not in body

    def test_ups_emits_ads(self, shiprush_client):
        # Default service catalog in the fixture maps UPS to UPS_GROUND.
        body = self._capture_xml(
            shiprush_client, self._payload_for_method('UPS Ground'),
            adult_signature=True,
        )
        assert '<DCISType>ADS</DCISType>' in body

    def test_usps_emits_ads(self, shiprush_client):
        # USPS service slot wired into the fixture via _resolve_carrier
        # default for any 'usps' substring.
        shiprush_client._settings.get.side_effect = lambda key, default=None: {
            'shiprush_accounts': {'USPS_EASYPOST': 'guid-usps'},
            'shiprush_services': {
                'USPS_GROUND_ADV': {'carrier_id': '18', 'account_key': 'USPS_EASYPOST',
                                    'service_code': 'USPSGNDADV', 'is_one_rate': False},
            },
            'shipper_origin': {'country': 'US'},
            'fallback_customer_phone': '',
        }.get(key, default if default is not None else {})
        body = self._capture_xml(
            shiprush_client, self._payload_for_method('USPS Ground Advantage'),
            adult_signature=True,
        )
        assert '<DCISType>ADS</DCISType>' in body

    def test_fedex_emits_f4(self, shiprush_client):
        shiprush_client._settings.get.side_effect = lambda key, default=None: {
            'shiprush_accounts': {'FEDEX': 'guid-fedex'},
            'shiprush_services': {
                'FEDEX_GROUND': {'carrier_id': '1', 'account_key': 'FEDEX',
                                 'service_code': 'F92', 'is_one_rate': False},
            },
            'shipper_origin': {'country': 'US'},
            'fallback_customer_phone': '',
        }.get(key, default if default is not None else {})
        body = self._capture_xml(
            shiprush_client, self._payload_for_method('FedEx Ground'),
            adult_signature=True,
        )
        assert '<DCISType>F4</DCISType>' in body


class TestShipRushCommodityEscaping:

    def test_xml_injection_in_description_is_escaped(self, shiprush_client):
        xml = shiprush_client._build_commodities_xml(
            [{
                'description': '<script>alert(1)</script>',
                'hs_code': '9507.30',
                'country_of_origin': 'US',
                'qty': 1,
                'unit_weight_oz': 5.4,
                'unit_value': 100.0,
            }], 'USD',
        )
        assert '<script>' not in xml
        assert '&lt;script&gt;' in xml


class TestShipRushCustomsValueSum:

    def test_sum_zero_for_empty(self):
        assert ShipRushClient._sum_customs_value([]) == 0

    def test_sum_skips_invalid(self):
        items = [
            {'qty': 1, 'unit_value': 'bad'},
            {'qty': 2, 'unit_value': 5.0},
        ]
        assert ShipRushClient._sum_customs_value(items) == 10.0


# ---------------------------------------------------------------------------
# Tracking-number prefix inference (international)
# ---------------------------------------------------------------------------


class TestIntlTrackingPrefixes:

    def test_dhl_express_10_digit(self):
        assert _carrier_from_tracking('1234567890') == 'DHL'

    def test_dhl_ecommerce_gm_prefix(self):
        assert _carrier_from_tracking('GM123456789') == 'DHL_ECOMMERCE'

    def test_s10_usps_intl_prefix(self):
        # CP / LM / RR / RA are USPS-handoff international prefixes.
        assert _carrier_from_tracking('CP123456789US') == 'USPS_INTL'
        assert _carrier_from_tracking('LM987654321US') == 'USPS_INTL'

    def test_s10_royal_mail_prefix(self):
        assert _carrier_from_tracking('LX123456789GB') == 'ROYAL_MAIL'

    def test_s10_canada_post_prefix(self):
        assert _carrier_from_tracking('EA123456789CA') == 'CANADA_POST'

    def test_s10_other_intl_falls_through(self):
        # Valid S10 shape but unknown prefix -> 'INTL'.
        assert _carrier_from_tracking('ZZ123456789DE') == 'INTL'

    def test_us_carriers_still_recognized(self):
        # Regression: domestic inference unchanged.
        assert _carrier_from_tracking('1Z999AA10123456784') == 'UPS'
        assert _carrier_from_tracking('9405511899223123456789') == 'USPS'


# ---------------------------------------------------------------------------
# SettingsStore: banned-country gate + public-subset secrecy
# ---------------------------------------------------------------------------


def _fresh_store(tmp_path):
    return SettingsStore(str(tmp_path / 'settings.json'))


class TestBannedCountryGate:

    def test_default_banned_list_seeded(self, tmp_path):
        store = _fresh_store(tmp_path)
        intl = store.get('international') or {}
        assert set(intl.get('banned_countries', [])) >= {'CU', 'IR', 'KP', 'SY'}

    def test_banned_country_returns_true(self, tmp_path):
        store = _fresh_store(tmp_path)
        assert store.is_country_banned('KP') is True
        assert store.is_country_banned('kp') is True  # case-insensitive
        assert store.is_country_banned(' kp ') is True  # whitespace-tolerant

    def test_allowed_country_returns_false(self, tmp_path):
        store = _fresh_store(tmp_path)
        assert store.is_country_banned('CA') is False
        assert store.is_country_banned('US') is False

    def test_blank_country_returns_false(self, tmp_path):
        store = _fresh_store(tmp_path)
        assert store.is_country_banned(None) is False
        assert store.is_country_banned('') is False

    def test_runtime_update_takes_effect(self, tmp_path):
        store = _fresh_store(tmp_path)
        assert store.is_country_banned('XX') is False
        store.patch({'international': {
            **DEFAULT_SETTINGS['international'],
            'banned_countries': ['XX'],
        }})
        assert store.is_country_banned('XX') is True


class TestPublicSubsetSecrecy:

    def test_public_subset_does_not_leak_tax_ids(self, tmp_path):
        store = _fresh_store(tmp_path)
        store.patch({'international': {
            **DEFAULT_SETTINGS['international'],
            'shipper_tax_ids': {
                'ein': 'SENSITIVE-EIN',
                'eori': 'SENSITIVE-EORI',
                'ioss': 'SENSITIVE-IOSS',
                'vat_uk': 'SENSITIVE-VAT',
            },
        }})
        subset = store.public_subset()
        flat = repr(subset)
        assert 'SENSITIVE-EIN' not in flat
        assert 'SENSITIVE-EORI' not in flat
        assert 'SENSITIVE-IOSS' not in flat
        assert 'SENSITIVE-VAT' not in flat
        assert 'shipper_tax_ids' not in subset
        # Operator UI still gets the enabled boolean so the intl pill
        # can render without admin scope.
        assert 'international_enabled' in subset

    def test_public_subset_does_not_leak_banned_list(self, tmp_path):
        store = _fresh_store(tmp_path)
        subset = store.public_subset()
        assert 'banned_countries' not in repr(subset)
