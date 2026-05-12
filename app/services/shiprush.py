"""ShipRush XML API client.

Handles label generation, voiding, and response parsing.

Token + endpoint are env-driven (Settings page writes them to .env);
shipper origin, account GUIDs, fallback phone, and the service catalog
live in SettingsStore so the admin can edit them without redeploying.
"""

import os
import time
import logging
import requests
from xml.sax.saxutils import escape

logger = logging.getLogger('dockd.shiprush')


class ShipRushClient:

    def __init__(self, settings_store, label_cache):
        self._settings = settings_store
        self.label_cache = label_cache

    # ---- env-backed credentials (Settings UI writes to .env) -----------

    @property
    def token(self):
        return (os.environ.get('SHIPRUSH_TOKEN') or '').strip()

    @property
    def endpoint(self):
        return os.environ.get('SHIPRUSH_ENDPOINT') or \
            'https://api.my.shiprush.com/shipmentservice.svc/shipment/ship'

    def _headers(self):
        return {
            'X-SHIPRUSH-SHIPPING-TOKEN': self.token,
            'Content-Type': 'application/xml',
            'User-Agent': 'Dockd_Shipping',
        }

    # ---- settings projections ------------------------------------------

    def _accounts(self):
        return self._settings.get('shiprush_accounts', {}) or {}

    def _services(self):
        return self._settings.get('shiprush_services', {}) or {}

    def _shipper_origin(self):
        return self._settings.get('shipper_origin', {}) or {}

    def _fallback_phone(self):
        return self._settings.get('fallback_customer_phone', '') or ''

    # ---- label generation ----------------------------------------------

    def generate_label(self, fulfillment_data, dims, weight_lbs,
                       packaging_code='02', order_number=None,
                       box_id=None, carrier_override=None):
        """Build and POST XML to ShipRush, return tracking + ZPL label."""
        addr = fulfillment_data.get('shippingAddress', {})
        cust = fulfillment_data.get('entity', {})
        origin = self._shipper_origin()

        order_num = escape(str(order_number)) if order_number \
            else escape(str(fulfillment_data.get('tranId', 'TEST')))

        name = escape(addr.get('addressee') or cust.get('refName') or 'Valued Customer')
        company = ''
        address1 = escape(addr.get('addr1', ''))
        address2 = escape(addr.get('addr2', ''))
        city = escape(addr.get('city', ''))
        state = escape(addr.get('state', ''))
        zip_code = escape(addr.get('zip', ''))
        phone = escape(addr.get('addrPhone', self._fallback_phone()))

        ns_method_raw = fulfillment_data.get('shipMethod', {}).get('refName', '')
        ns_method = ns_method_raw.lower()
        logger.info("Shipping method detected: '%s'", ns_method_raw)

        carrier_id, account_key, service_code, is_one_rate = \
            self._resolve_carrier(ns_method, carrier_override)

        selected_guid = self._accounts().get(account_key, '')
        shipping_account_block = ''
        if selected_guid:
            shipping_account_block = (
                f'<ShippingAccount><ShippingAccountId>{selected_guid}'
                f'</ShippingAccountId></ShippingAccount>'
            )

        service_tag = f'<UPSServiceType>{service_code}</UPSServiceType>'

        package_ref2 = ''
        if box_id is not None:
            pkg_descr = f"Box {escape(str(box_id))} {dims['l']}x{dims['w']}x{dims['h']}"
            package_ref2 = f'<PackageReference2>{pkg_descr}</PackageReference2>'

        xml_payload = f"""<?xml version="1.0" encoding="utf-8"?>
<ShipRequest xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <ShipTransaction>
    <Shipment>
      <OrderNum>{order_num}</OrderNum>
      <ShipDate>{time.strftime('%Y-%m-%d')}</ShipDate>
      <Carrier>{carrier_id}</Carrier>
      {service_tag}
      {shipping_account_block}
      <FedExOneRate>{'true' if is_one_rate else 'false'}</FedExOneRate>
      <ShipperAddress>
        <Address>
          <Company>{escape(origin.get('company', ''))}</Company>
          <Address1>{escape(origin.get('address1', ''))}</Address1>
          <Address2>{escape(origin.get('address2', ''))}</Address2>
          <City>{escape(origin.get('city', ''))}</City>
          <State>{escape(origin.get('state', ''))}</State>
          <PostalCode>{escape(origin.get('postal_code', ''))}</PostalCode>
          <Country>{escape(origin.get('country', 'US'))}</Country>
          <Phone>{escape(origin.get('phone', ''))}</Phone>
        </Address>
      </ShipperAddress>
      <DeliveryAddress>
        <Address>
          <FirstName>{name}</FirstName>
          <Company>{company}</Company>
          <Address1>{address1}</Address1>
          <Address2>{address2}</Address2>
          <City>{city}</City>
          <State>{state}</State>
          <PostalCode>{zip_code}</PostalCode>
          <Country>US</Country>
          <Phone>{phone}</Phone>
        </Address>
      </DeliveryAddress>
      <Package>
        <PackageActualWeight>{weight_lbs}</PackageActualWeight>
        <PackagingType>{packaging_code}</PackagingType>
        <PkgLength>{dims['l']}</PkgLength>
        <PkgWidth>{dims['w']}</PkgWidth>
        <PkgHeight>{dims['h']}</PkgHeight>
        <PackageReference1>{order_num}</PackageReference1>
        {package_ref2}
      </Package>
    </Shipment>
  </ShipTransaction>
  <ShipSettings>
    <PrinterShippingLabel><LabelType>ZPL</LabelType></PrinterShippingLabel>
    <AddressOption>3</AddressOption>
  </ShipSettings>
</ShipRequest>"""

        logger.info("ShipRush: sending XML (ref: %s, carrier %s)", order_num, carrier_id)

        try:
            r = requests.post(self.endpoint, data=xml_payload,
                              headers=self._headers(), timeout=25)
            return self._parse_response(r.text, order_num)
        except Exception as e:
            return {'status': 'error', 'message': self.friendly_error(e)}

    def void_label(self, shipment_id):
        """Void a label via ShipRush API."""
        endpoint = self.endpoint
        if '/ship' in endpoint:
            void_endpoint = endpoint.replace('/shipment/ship', '/shipment/void')
            if void_endpoint == endpoint:
                void_endpoint = endpoint.replace('/ship', '/void')
        else:
            void_endpoint = f'{endpoint}/void'

        xml_payload = f"""<?xml version="1.0" encoding="utf-8"?>
<VoidRequest xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
    <ShipTransaction>
        <Shipment>
            <ShipmentId>{shipment_id}</ShipmentId>
        </Shipment>
    </ShipTransaction>
</VoidRequest>"""

        try:
            r = requests.post(void_endpoint, data=xml_payload,
                              headers=self._headers(), timeout=20)
            if r.status_code == 200 and '<IsSuccess>true</IsSuccess>' in r.text:
                return {'status': 'success', 'message': 'Label voided successfully.'}
            if 'already voided' in r.text.lower():
                return {'status': 'success', 'message': 'Label was already voided.'}
            return {'status': 'error', 'message': self.friendly_error(r.text)}
        except Exception as e:
            return {'status': 'error', 'message': self.friendly_error(e)}

    # ---- carrier/service resolution ------------------------------------

    # Fallback tuple when shiprush_services has no entry that matches.
    # The four values follow the existing convention: carrier_id '1'
    # (UPS), account_key 'UPS', service_code '03' (UPS Ground),
    # is_one_rate False. Callers always unpack a 4-tuple so this is
    # the safe shape under empty / under-configured settings.
    _RESOLVE_FALLBACK = ('1', 'UPS', '03', False)

    def _resolve_carrier(self, ns_method, carrier_override):
        """Pick a row from `shiprush_services` based on override or
        ship-method substring matching. Always returns a 4-tuple
        (carrier_id, account_key, service_code, is_one_rate); falls
        back to `_RESOLVE_FALLBACK` when no slot matches so the
        caller never has to handle `None`."""
        services = self._services()

        def row(slot):
            r = services.get(slot)
            if not r:
                return None
            return (
                str(r.get('carrier_id', '1')),
                str(r.get('account_key', 'UPS')),
                str(r.get('service_code', '03')),
                bool(r.get('is_one_rate', False)),
            )

        def _resolve_or_fallback(*slots):
            for slot in slots:
                got = row(slot)
                if got is not None:
                    return got
            return self._RESOLVE_FALLBACK

        if carrier_override == 'UPS':
            return _resolve_or_fallback('UPS_GROUND')
        if carrier_override == 'USPS':
            return _resolve_or_fallback('USPS_GROUND_ADV')
        if carrier_override == 'FEDEX_ONE_RATE_2DAY':
            return _resolve_or_fallback('FEDEX_ONE_RATE')

        method = (ns_method or '').lower()

        if 'usps' in method or 'post' in method or 'media' in method:
            if 'priority' in method:
                return _resolve_or_fallback('USPS_PRIORITY', 'USPS_GROUND_ADV')
            if 'media' in method:
                return _resolve_or_fallback('USPS_MEDIA_MAIL', 'USPS_GROUND_ADV')
            if 'first' in method:
                return _resolve_or_fallback('USPS_FIRST_CLASS', 'USPS_GROUND_ADV')
            if 'parcel' in method:
                return _resolve_or_fallback('USPS_PARCEL', 'USPS_GROUND_ADV')
            return _resolve_or_fallback('USPS_GROUND_ADV', 'UPS_GROUND')

        if 'fedex' in method:
            if '2day' in method or '2 day' in method or 'second day' in method:
                return _resolve_or_fallback('FEDEX_2DAY', 'FEDEX_GROUND')
            if 'overnight' in method:
                return _resolve_or_fallback('FEDEX_OVERNIGHT', 'FEDEX_GROUND')
            if 'one rate' in method or 'onerate' in method:
                return _resolve_or_fallback('FEDEX_ONE_RATE', 'FEDEX_GROUND')
            return _resolve_or_fallback('FEDEX_GROUND')

        if 'next day' in method:
            return _resolve_or_fallback('UPS_NEXT_DAY', 'UPS_GROUND')
        if '2nd day' in method:
            return _resolve_or_fallback('UPS_2ND_DAY', 'UPS_GROUND')
        if '3 day' in method:
            return _resolve_or_fallback('UPS_3_DAY', 'UPS_GROUND')

        return _resolve_or_fallback('UPS_GROUND')

    # ---- response parsing + friendly error -----------------------------

    def _parse_response(self, response_text, order_num):
        resp = response_text

        def _extract(tag):
            if f'<{tag}>' in resp:
                try:
                    return resp.split(f'<{tag}>')[1].split(f'</{tag}>')[0]
                except IndexError:
                    pass
            return ''

        if '<IsSuccess>true</IsSuccess>' in resp or \
                ('<TrackingNumber>' in resp and '<ContentMimeEncoded>' in resp):

            tracking = _extract('TrackingNumber') or _extract('PackageTrackingNumber')
            label_b64 = _extract('ContentMimeEncoded')
            shipment_id = _extract('ShipmentId')

            carrier_rate = None
            rate_str = _extract('CarrierRate')
            if rate_str:
                try:
                    carrier_rate = float(rate_str)
                except ValueError:
                    pass

            if tracking and label_b64:
                if '<IsSuccess>false</IsSuccess>' in resp:
                    logger.warning("ShipRush IsSuccess=false but label exists - accepting")
                self.label_cache.save(order_num, shipment_id, tracking, label_b64)
                return {
                    'status': 'success',
                    'tracking': tracking,
                    'zpl_b64': label_b64,
                    'cost': carrier_rate,
                }

            return {
                'status': 'error',
                'message': 'Label response was missing tracking or label data. Try again.',
            }

        logger.error("ShipRush label failed: %s", resp[:800])
        return {'status': 'error', 'message': self.friendly_error(resp)}

    @staticmethod
    def friendly_error(text_or_exception):
        text = text_or_exception if isinstance(text_or_exception, str) else str(text_or_exception)
        lower = text.lower()

        if 'not selected' in lower and 'fedex' in lower:
            return 'Please select a valid FedEx service for this order.'
        if 'not selected' in lower:
            return 'Shipping service not selected or invalid. Check service and try again.'
        if 'address' in lower and ('valid' in lower or 'modif' in lower):
            return 'Address was corrected by the carrier. Check the label and try again.'
        if 'invalid' in lower or 'not valid' in lower:
            return 'Invalid shipping details. Check address, service, and packaging.'
        if 'timeout' in lower or 'timed out' in lower:
            return 'Shipping service timed out. Try again.'
        if 'connection' in lower or 'connect' in lower:
            return 'Could not reach shipping service. Check network and try again.'

        if '<text>' in lower and '</text>' in lower:
            try:
                start = lower.index('<text>') + 6
                end = lower.index('</text>')
                snippet = text[start:end].strip()
                if snippet and len(snippet) < 200:
                    return snippet
            except Exception:
                pass

        if len(text) > 250:
            return 'Shipping service error. Try again or check your ShipRush account.'
        return text.strip() or 'Shipping service error. Please try again.'
