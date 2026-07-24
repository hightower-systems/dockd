"""ShipRushClient.rate_shop() -- read-only multi-carrier quoting (v2).

The XML fixtures below are trimmed from a real /shipment/rateshopping
response captured live against the AvidMax account on 2026-07-22
(Aurora CO -> Bozeman MT, 4.82 lb, 12x9x6). Field names, casing, and the
trademark glyphs in service names are reproduced exactly, because those are
what the parser actually has to survive.

No test here touches the network.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.shiprush import ShipRushClient


def _svc(name, code, rate, days, one_rate='false', total=None, extra=''):
    return f"""<AvailableService>
      <ShippingAccountId>a1b2c3</ShippingAccountId>
      <Name>{name}</Name>
      <ServiceType>{code}</ServiceType>
      <PackagingType>02</PackagingType>
      <OneRate>{one_rate}</OneRate>
      <Currency>USD</Currency>
      <CarrierRate>{rate}</CarrierRate>
      <Markup>0</Markup>
      <Total>{total if total is not None else rate}</Total>
      <TimeInTransitText>{days} business days</TimeInTransitText>
      <TimeInTransitDays>{days}</TimeInTransitDays>
      <TimeInTransitBusinessDays>{days}</TimeInTransitBusinessDays>
      <ExpectedDelivery>2026-07-24</ExpectedDelivery>
      <IsEstimated>false</IsEstimated>
      <ShipmentQuoteId>Q-{code}</ShipmentQuoteId>
      {extra}
    </AvailableService>"""


LIVE_SHAPE = f"""<?xml version="1.0" encoding="utf-8"?>
<RateShoppingResponse>
  <AvailableServices>
    {_svc('UPS&#174; Ground', '03', '14.36', 2)}
    {_svc('USPS Ground Advantage&#8482;', 'USPSGNDADV', '8.71', 2)}
    {_svc('FedEx Ground&#174; Economy', 'FSP', '10.5', 5)}
    {_svc('FedEx 2Day&#174;', 'F03', '20.06', 2, one_rate='true')}
  </AvailableServices>
</RateShoppingResponse>"""


@pytest.fixture
def client():
    settings = MagicMock()
    settings.get.side_effect = lambda key, *a: {
        'shipper_origin': {
            'company': 'AvidMax', 'address1': '1 Test Way', 'address2': '',
            'city': 'Aurora', 'state': 'CO', 'postal_code': '80012',
            'country': 'US', 'phone': '3035550100',
        },
        'fallback_customer_phone': '3035550100',
    }.get(key, {})
    return ShipRushClient(settings, MagicMock())


ADDR = {
    'addressee': 'Dana Whitfield', 'addr1': '1420 N Willow Creek Rd',
    'city': 'Bozeman', 'state': 'MT', 'zip': '59718', 'country': 'US',
}
DIMS = {'l': 12, 'w': 9, 'h': 6}


class TestParsing:

    def test_returns_every_service(self, client):
        out = client._parse_rate_response(LIVE_SHAPE)
        assert out['status'] == 'success'
        assert len(out['services']) == 4

    def test_sorted_cheapest_first(self, client):
        out = client._parse_rate_response(LIVE_SHAPE)
        assert [s['total'] for s in out['services']] == [8.71, 10.5, 14.36, 20.06]

    def test_carries_transit_days(self, client):
        """Transit is the whole reason cheapest is not automatically right:
        FSP is $10.50 at 5 days while USPS GA is $8.71 at 2."""
        by_code = {s['service_code']: s for s in
                   client._parse_rate_response(LIVE_SHAPE)['services']}
        assert by_code['USPSGNDADV']['transit_days'] == 2
        assert by_code['FSP']['transit_days'] == 5

    def test_ground_economy_is_priced(self, client):
        """FSP cannot be rated through /shipment/rate at all; the whole
        reason rate_shop uses /rateshopping is that it prices FSP anyway."""
        codes = [s['service_code'] for s in
                 client._parse_rate_response(LIVE_SHAPE)['services']]
        assert 'FSP' in codes

    def test_one_rate_flag_survives(self, client):
        by_code = {s['service_code']: s for s in
                   client._parse_rate_response(LIVE_SHAPE)['services']}
        assert by_code['F03']['one_rate'] is True
        assert by_code['03']['one_rate'] is False

    def test_quote_id_captured(self, client):
        out = client._parse_rate_response(LIVE_SHAPE)
        assert all(s['quote_id'] for s in out['services'])

    def test_total_wins_over_carrier_rate_when_marked_up(self, client):
        xml = f"<AvailableServices>{_svc('UPS', '03', '10.00', 2, total='12.50')}</AvailableServices>"
        out = client._parse_rate_response(xml)
        assert out['services'][0]['total'] == 12.50
        assert out['services'][0]['carrier_rate'] == 10.00

    def test_unpriced_service_is_dropped(self, client):
        """A row with no price is not a choice. Better to omit it than to
        render a blank cost at the pack bench."""
        broken = """<AvailableServices><AvailableService>
            <Name>Mystery</Name><ServiceType>ZZZ</ServiceType>
        </AvailableService></AvailableServices>"""
        assert client._parse_rate_response(broken)['status'] == 'error'

    def test_empty_response_is_an_error_not_a_crash(self, client):
        assert client._parse_rate_response('<RateShoppingResponse/>')['status'] == 'error'


class TestRequestContract:

    def _post_mock(self, body='<RateShoppingResponse/>', status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.text = body
        return resp

    def test_version_header_is_numeric(self, client):
        """ShipRush 500s on 'v100' with 'Number expected.' Verified live."""
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock(LIVE_SHAPE)
            client.rate_shop(ADDR, DIMS, 4.82)
        headers = post.call_args.kwargs['headers']
        assert headers['X-SHIPRUSH-VERSION'] == '100'
        assert not headers['X-SHIPRUSH-VERSION'].startswith('v')

    def test_version_header_not_added_to_the_label_path(self, client):
        """Shared _headers() feeds /shipment/ship, the live label buy. The
        SDK header belongs to rate_shop only, so adding rating cannot change
        label behaviour as a side effect."""
        assert 'X-SHIPRUSH-VERSION' not in client._headers()

    def test_hits_rateshopping_not_ship(self, client):
        """A typo here would buy a label instead of quoting one."""
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock(LIVE_SHAPE)
            client.rate_shop(ADDR, DIMS, 4.82)
        url = post.call_args.args[0]
        assert url.endswith('/shipment/rateshopping')
        assert '/shipment/ship' not in url

    def test_omits_carrier_so_all_accounts_return(self, client):
        """With no <Carrier> and no <UPSServiceType>, ShipRush returns every
        service across every provisioned account in one POST."""
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock(LIVE_SHAPE)
            client.rate_shop(ADDR, DIMS, 4.82)
        body = post.call_args.kwargs['data'].decode('utf-8')
        assert '<Carrier>' not in body
        assert '<UPSServiceType>' not in body
        assert '<RateShoppingRequest' in body

    def test_dims_and_weight_are_sent(self, client):
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock(LIVE_SHAPE)
            client.rate_shop(ADDR, DIMS, 4.82)
        body = post.call_args.kwargs['data'].decode('utf-8')
        assert '<PackageActualWeight>4.82</PackageActualWeight>' in body
        assert '<PackageLength>12</PackageLength>' in body
        assert '<PackageWidth>9</PackageWidth>' in body
        assert '<PackageHeight>6</PackageHeight>' in body

    def test_destination_is_escaped(self, client):
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock(LIVE_SHAPE)
            client.rate_shop(dict(ADDR, addressee='Ben & Jerry <test>'), DIMS, 4.82)
        body = post.call_args.kwargs['data'].decode('utf-8')
        assert '&amp;' in body
        assert '<test>' not in body

    def test_zero_weight_floors_to_a_shippable_value(self, client):
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock(LIVE_SHAPE)
            client.rate_shop(ADDR, DIMS, 0)
        body = post.call_args.kwargs['data'].decode('utf-8')
        assert '<PackageActualWeight>0</PackageActualWeight>' not in body


class TestFailuresDegradeQuietly:
    """rate_shop failing must never block a ship. Every path returns an
    error dict so ShippingService can fall back to the carrier engine."""

    def test_network_error_returns_error_dict(self, client):
        with patch('app.services.shiprush.requests.post', side_effect=OSError('boom')):
            out = client.rate_shop(ADDR, DIMS, 4.82)
        assert out['status'] == 'error'
        assert 'message' in out

    def test_timeout_is_bounded(self, client):
        with patch('app.services.shiprush.requests.post') as post:
            post.return_value = self._post_mock_ok()
            client.rate_shop(ADDR, DIMS, 4.82)
        assert post.call_args.kwargs['timeout'] == ShipRushClient.RATE_TIMEOUT
        assert ShipRushClient.RATE_TIMEOUT <= 15

    def test_http_500_returns_error_dict(self, client):
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "<Error><Message>Invalid SDK version HTTP header 'v100'.</Message></Error>"
        with patch('app.services.shiprush.requests.post', return_value=resp):
            out = client.rate_shop(ADDR, DIMS, 4.82)
        assert out['status'] == 'error'

    def _post_mock_ok(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = LIVE_SHAPE
        return resp


class TestDryRun:
    """DOCKD_DRY_RUN_LABELS stubs label BUYS only, so a test rig can carry a
    real token (rating is read-only and free) without a click in a carrier
    modal buying real postage to whatever address the fixture used."""

    def test_off_by_default(self, client, monkeypatch):
        monkeypatch.delenv('DOCKD_DRY_RUN_LABELS', raising=False)
        assert client.dry_run is False

    def test_malformed_value_is_off(self, client, monkeypatch):
        """Production must not acquire this behaviour by accident."""
        for val in ('', 'maybe', '0', 'false', 'no'):
            monkeypatch.setenv('DOCKD_DRY_RUN_LABELS', val)
            assert client.dry_run is False, val

    def test_truthy_values_enable_it(self, client, monkeypatch):
        for val in ('1', 'true', 'TRUE', 'yes', 'on'):
            monkeypatch.setenv('DOCKD_DRY_RUN_LABELS', val)
            assert client.dry_run is True, val

    def test_dry_run_never_calls_shiprush(self, client, monkeypatch):
        monkeypatch.setenv('DOCKD_DRY_RUN_LABELS', 'true')
        with patch('app.services.shiprush.requests.post') as post:
            out = client.generate_label({'shippingAddress': {}, 'entity': {}},
                                        {'l': 12, 'w': 9, 'h': 6}, 4.82,
                                        order_number='104829')
        post.assert_not_called()
        assert out['status'] == 'success'
        assert out['dry_run'] is True
        assert out['tracking'].startswith('DRYRUN')

    def test_dry_run_still_returns_printable_zpl(self, client, monkeypatch):
        """The downstream path (print, confirm, history, reprint) must stay
        exercised, so the stub returns a real base64 ZPL rather than None."""
        import base64
        monkeypatch.setenv('DOCKD_DRY_RUN_LABELS', 'true')
        out = client.generate_label({'shippingAddress': {}, 'entity': {}},
                                    {'l': 12, 'w': 9, 'h': 6}, 4.82,
                                    order_number='104829')
        assert '^XA' in base64.b64decode(out['zpl_b64']).decode()

    def test_rating_is_unaffected_by_dry_run(self, client, monkeypatch):
        """Dry run stubs BUYS, not quotes. Rates must stay real."""
        monkeypatch.setenv('DOCKD_DRY_RUN_LABELS', 'true')
        with patch('app.services.shiprush.requests.post') as post:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = LIVE_SHAPE
            post.return_value = resp
            out = client.rate_shop(ADDR, DIMS, 4.82, include_one_rate=False)
        post.assert_called_once()
        assert out['status'] == 'success'


class TestOneRateAndSuppression:
    """One Rate is flat-rate pricing that only exists inside FedEx-branded
    packaging. Verified live: with PackagingType 02 the flag returns nothing
    One Rate at all; with a FedEx box, Aurora -> Montpelier quoted 2Day at
    $12.79 One Rate against $33.59 dimensional."""

    def _resp(self, body):
        r = MagicMock()
        r.status_code = 200
        r.text = body
        return r

    def test_ups_ground_saver_is_never_offered(self, client):
        xml = f"<AvailableServices>{_svc('UPS Ground Saver', 'UPSGROUNDSAVER', '9.99', 4)}"\
              f"{_svc('UPS Ground', '03', '14.36', 2)}</AvailableServices>"
        with patch('app.services.shiprush.requests.post', return_value=self._resp(xml)):
            out = client.rate_shop(ADDR, DIMS, 4.82, include_one_rate=False)
        codes = [s['service_code'] for s in out['services']]
        assert 'UPSGROUNDSAVER' not in codes
        assert '03' in codes

    def test_one_rate_quote_is_a_second_call_with_fedex_packaging(self, client):
        one = f"<AvailableServices>{_svc('FedEx 2Day', 'F03', '12.79', 2, one_rate='true')}</AvailableServices>"
        with patch('app.services.shiprush.requests.post') as post:
            post.side_effect = [self._resp(LIVE_SHAPE), self._resp(one)]
            client.rate_shop(ADDR, DIMS, 4.82)
        assert post.call_count == 2
        second = post.call_args_list[1].kwargs['data'].decode('utf-8')
        assert '<FedExOneRate>true</FedExOneRate>' in second
        assert f'<PackagingType>{ShipRushClient.ONE_RATE_PACKAGING}</PackagingType>' in second

    def test_one_rate_row_is_labelled_and_flagged(self, client):
        one = f"<AvailableServices>{_svc('FedEx 2Day', 'F03', '12.79', 2, one_rate='true')}</AvailableServices>"
        with patch('app.services.shiprush.requests.post') as post:
            post.side_effect = [self._resp(LIVE_SHAPE), self._resp(one)]
            out = client.rate_shop(ADDR, DIMS, 4.82)
        row = next(s for s in out['services'] if s.get('requires_fedex_box'))
        assert 'One Rate' in row['name']
        assert row['total'] == 12.79

    def test_only_2day_one_rate_is_pulled_in(self, client):
        """Express and Overnight One Rate tiers are not what this bench ships."""
        one = ("<AvailableServices>"
               + _svc('FedEx 2Day', 'F03', '12.79', 2, one_rate='true')
               + _svc('FedEx Priority Overnight', 'F01', '132.40', 1, one_rate='true')
               + "</AvailableServices>")
        with patch('app.services.shiprush.requests.post') as post:
            post.side_effect = [self._resp(LIVE_SHAPE), self._resp(one)]
            out = client.rate_shop(ADDR, DIMS, 4.82)
        one_rate_codes = [s['service_code'] for s in out['services'] if s.get('requires_fedex_box')]
        assert one_rate_codes == ['F03']

    def test_a_failed_one_rate_call_does_not_break_the_quote(self, client):
        """The One Rate row is a bonus, never a dependency."""
        with patch('app.services.shiprush.requests.post') as post:
            post.side_effect = [self._resp(LIVE_SHAPE), OSError('boom')]
            out = client.rate_shop(ADDR, DIMS, 4.82)
        assert out['status'] == 'success'
        assert len(out['services']) == 4
