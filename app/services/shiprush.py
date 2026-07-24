"""ShipRush XML API client.

Handles label generation, voiding, and response parsing.

Token + endpoint are env-driven (Settings page writes them to .env);
shipper origin, account GUIDs, fallback phone, and the service catalog
live in SettingsStore so the admin can edit them without redeploying.
"""

import base64
import os
import re
import time
import logging
import requests
from xml.sax.saxutils import escape

logger = logging.getLogger('dockd.shiprush')


class ShipRushClient:

    # Rate shopping now sits in the critical path of every ship, between the
    # box scan and the label buy, with an operator standing at the bench. Kept
    # tighter than the 25s label timeout on purpose: a slow quote must not
    # stall a ship, and ShippingService falls back to the carrier engine when
    # this returns an error. Observed live: ~1.4s for 18 services.
    RATE_TIMEOUT = 12

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

    def service_code_for(self, ns_method, carrier_override=None):
        """The service code this ship method would buy, without buying it.

        Thin public wrapper over the same _resolve_carrier() the label path
        uses, so rate selection compares against the service that would
        actually be purchased rather than a second guess at it. If these two
        ever disagreed, rate shopping would be measuring the wrong baseline.
        """
        return self._resolve_carrier((ns_method or '').lower(), carrier_override)[2]

    # ---- rate shopping (read-only) -------------------------------------

    # ShipRush version-gates its enums behind an SDK version header, and it
    # must be a bare number: sending 'v100' returns HTTP 500 "Invalid SDK
    # version HTTP header 'v100'. Number expected." Verified live against
    # the AvidMax account on 2026-07-22.
    #
    # Sent per call rather than from _headers() on purpose. /shipment/ship
    # is the live label path and has worked for years without this header;
    # adding it there would be an unrelated behaviour change riding along
    # with a UI branch.
    SDK_VERSION = '100'

    # UPS Ground Saver is intentionally never offered: it is a UPS-injected
    # USPS-final-mile product that competes with the USPS services already
    # quoted, at worse transit, and it muddies a three-carrier comparison.
    SUPPRESSED_SERVICES = {'UPSGROUNDSAVER'}

    # FedEx One Rate is flat-rate pricing that only exists inside FedEx-branded
    # packaging. Sending <FedExOneRate>true</FedExOneRate> with the customer's
    # own box (PackagingType 02) returns nothing One Rate at all -- verified
    # live. With a FedEx box it can beat the dimensional rate several times
    # over: Aurora -> Montpelier quoted 2Day at $12.79 One Rate against $33.59
    # dimensional. Only 2Day is pulled in; the Express/Overnight One Rate tiers
    # are not services this bench ships.
    ONE_RATE_PACKAGING = '04'
    ONE_RATE_SERVICES = {'F03'}

    def _rate_payload(self, delivery_address, dims, weight_lbs, packaging_code,
                      one_rate=False):
        """Build a RateShoppingRequest. Shared by the standard and One Rate
        quotes so the two can never drift apart on address or dims."""
        origin = self._shipper_origin()
        addr = delivery_address or {}
        weight = max(float(weight_lbs or 0), 0.0625)
        dims = dims or {}
        one_rate_flag = '<FedExOneRate>true</FedExOneRate>' if one_rate else ''
        return f"""<?xml version="1.0" encoding="utf-8"?>
<RateShoppingRequest xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <ShipTransaction>
    <Shipment>
      {one_rate_flag}
      <Package>
        <PackageActualWeight>{weight}</PackageActualWeight>
        <PackagingType>{escape(str(packaging_code))}</PackagingType>
        <PackageLength>{dims.get('l', '')}</PackageLength>
        <PackageWidth>{dims.get('w', '')}</PackageWidth>
        <PackageHeight>{dims.get('h', '')}</PackageHeight>
      </Package>
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
          <FirstName>{escape(addr.get('addressee') or 'Valued Customer')}</FirstName>
          <Address1>{escape(addr.get('addr1', ''))}</Address1>
          <Address2>{escape(addr.get('addr2', ''))}</Address2>
          <City>{escape(addr.get('city', ''))}</City>
          <State>{escape(addr.get('state', ''))}</State>
          <PostalCode>{escape(addr.get('zip', ''))}</PostalCode>
          <Country>{escape(str(addr.get('country') or 'US').upper()[:2])}</Country>
          <Phone>{escape(addr.get('addrPhone') or self._fallback_phone())}</Phone>
        </Address>
      </DeliveryAddress>
    </Shipment>
  </ShipTransaction>
</RateShoppingRequest>"""

    def rate_shop(self, delivery_address, dims, weight_lbs,
                  packaging_code='02', include_one_rate=True):
        """Price every provisioned service for one package. Buys nothing.

        Deliberately omits <Carrier> and <UPSServiceType>: with neither
        present ShipRush returns every service across every shipping
        account in a single POST, which is both cheaper than N calls and
        the only way to price FedEx Ground Economy (FSP). /shipment/rate
        rejects FSP with "Please select Ground Economy Hub ID" because the
        hub is account-level config, but /shipment/rateshopping returns a
        price for it regardless.

        Returns {'status': 'success', 'services': [...]} sorted cheapest
        first, or {'status': 'error', 'message': ...}. Callers must treat
        an error as "no quotes available" and fall back to the carrier
        engine, never as a reason to block a ship.
        """
        payload = self._rate_payload(delivery_address, dims, weight_lbs, packaging_code)

        out = self._post_rates(payload)
        if out.get('status') != 'success':
            return out

        out['services'] = [
            s for s in out['services']
            if str(s.get('service_code', '')).upper() not in self.SUPPRESSED_SERVICES
        ]

        if include_one_rate:
            out['services'].extend(self._one_rate_services(delivery_address, dims, weight_lbs))
            out['services'].sort(key=lambda s: s['total'])

        return out

    def _post_rates(self, payload):
        endpoint = self.endpoint.replace('/shipment/ship', '/shipment/rateshopping')
        headers = dict(self._headers())
        headers['X-SHIPRUSH-VERSION'] = self.SDK_VERSION
        try:
            resp = requests.post(endpoint, data=payload.encode('utf-8'),
                                 headers=headers, timeout=self.RATE_TIMEOUT)
        except Exception as exc:
            logger.warning("Rate shop request failed: %s", exc)
            return {'status': 'error', 'message': self.friendly_error(exc)}
        if resp.status_code != 200:
            logger.warning("Rate shop HTTP %s: %s", resp.status_code, resp.text[:400])
            return {'status': 'error', 'message': self.friendly_error(resp.text)}
        return self._parse_rate_response(resp.text)

    def _one_rate_services(self, delivery_address, dims, weight_lbs):
        """Second quote for FedEx One Rate. Never fatal.

        A separate call because One Rate needs both the flag and FedEx
        packaging, which changes the request for every other carrier too.
        Failure here just means the One Rate row is absent.
        """
        try:
            payload = self._rate_payload(delivery_address, dims, weight_lbs,
                                         self.ONE_RATE_PACKAGING, one_rate=True)
            out = self._post_rates(payload)
            if out.get('status') != 'success':
                return []
            picked = [
                dict(s, requires_fedex_box=True)
                for s in out['services']
                if s.get('one_rate')
                and str(s.get('service_code', '')).upper() in self.ONE_RATE_SERVICES
            ]
            for s in picked:
                s['name'] = f"{s['name']} (One Rate)"
            return picked
        except Exception as exc:
            logger.warning("One Rate quote failed, continuing without it: %s", exc)
            return []

    @staticmethod
    def _parse_rate_response(response_text):
        """Pull <AvailableService> blocks out of a RateShoppingResponse.

        Same regex-over-XML approach as _parse_response(); ShipRush's
        payloads are flat and predictable, and the existing label parser
        has run this way in production since v0.1.
        """
        blocks = re.findall(
            r'<AvailableService>(.*?)</AvailableService>', response_text, re.S
        )
        if not blocks:
            logger.warning("Rate shop returned no services: %s", response_text[:400])
            return {'status': 'error', 'message': 'No rates were returned for this package.'}

        def tag(block, name):
            m = re.search(rf'<{name}>(.*?)</{name}>', block, re.S)
            return m.group(1).strip() if m else ''

        def num(block, name, cast=float, default=None):
            raw = tag(block, name)
            if not raw:
                return default
            try:
                return cast(raw)
            except ValueError:
                return default

        services = []
        for b in blocks:
            total = num(b, 'Total')
            carrier_rate = num(b, 'CarrierRate')
            cost = total if total is not None else carrier_rate
            if cost is None:
                # A service with no price is not a choice; drop it rather
                # than surface a blank row at the pack bench.
                continue
            services.append({
                'name': tag(b, 'Name'),
                'service_code': tag(b, 'ServiceType'),
                'packaging_type': tag(b, 'PackagingType'),
                'account_id': tag(b, 'ShippingAccountId'),
                'carrier_rate': carrier_rate,
                'markup': num(b, 'Markup', default=0.0),
                'total': cost,
                'transit_days': num(b, 'TimeInTransitDays', int),
                'transit_business_days': num(b, 'TimeInTransitBusinessDays', int),
                'transit_text': tag(b, 'TimeInTransitText'),
                'expected_delivery': tag(b, 'ExpectedDelivery'),
                'is_estimated': tag(b, 'IsEstimated').lower() == 'true',
                'one_rate': tag(b, 'OneRate').lower() == 'true',
                'currency': tag(b, 'Currency') or 'USD',
                'quote_id': tag(b, 'ShipmentQuoteId'),
            })

        if not services:
            return {'status': 'error', 'message': 'No priced rates were returned for this package.'}

        services.sort(key=lambda s: s['total'])
        return {'status': 'success', 'services': services}

    # ---- label generation ----------------------------------------------

    @property
    def dry_run(self):
        """True when label BUYS are stubbed out. Rating is unaffected.

        Exists because a realistic test rig needs a real ShipRush token
        (rate shopping is read-only, so quotes are free and safe) while a
        single click in a carrier modal would otherwise buy a real,
        billable label to whatever address the test data used.

        Off unless explicitly enabled, so production cannot acquire this
        behaviour by accident: an unset or malformed value is False.
        """
        return (os.environ.get('DOCKD_DRY_RUN_LABELS') or '').strip().lower() in (
            '1', 'true', 'yes', 'on')

    def generate_label(self, fulfillment_data, dims, weight_lbs,
                       packaging_code='02', order_number=None,
                       box_id=None, carrier_override=None,
                       adult_signature=False):
        """Build and POST XML to ShipRush, return tracking + ZPL label."""
        if self.dry_run:
            # Deliberately mimics a success response, including caching, so
            # the whole downstream path is still exercised: printing,
            # confirm_shipped to Sentry, history write, reprint and void.
            # A stub that returned early would leave those untested.
            tracking = f"DRYRUN{int(time.time())}"
            zpl = base64.b64encode(
                f"^XA^FO50,50^A0N,40,40^FDDRY RUN {order_number or ''}^FS^XZ".encode()
            ).decode()
            logger.warning(
                "DRY RUN: no label bought for order %s. Unset "
                "DOCKD_DRY_RUN_LABELS to buy real postage.", order_number,
            )
            self.label_cache.save(order_number, tracking, tracking, zpl)
            return {'status': 'success', 'tracking': tracking,
                    'zpl_b64': zpl, 'cost': None, 'dry_run': True}

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

        # International (v0.7.0): the upstream payload now carries
        # destination country and per-item customs data. Anything
        # other than 'US' is treated as international and triggers
        # a <Commodities> block + <CustomsValue> + <IncotermsCode>.
        # ShippingService is responsible for blocking banned
        # destinations before this method is ever called; this
        # builder just translates whatever it gets.
        dest_country_raw = str(addr.get('country') or 'US').strip().upper()[:2] or 'US'
        dest_country = escape(dest_country_raw)
        is_international = dest_country_raw != 'US'
        customs_items = fulfillment_data.get('customs_items') or []
        currency_raw = str(fulfillment_data.get('currency') or 'USD').strip().upper()[:3] or 'USD'
        currency = escape(currency_raw)
        duty_payer_raw = (fulfillment_data.get('duties_paid_by') or '').strip().lower()
        # ShipRush <IncotermsCode>: DAP = recipient pays duty (DDU);
        # DDP = sender pays. Default to DAP (the more common case).
        incoterms = 'DDP' if duty_payer_raw == 'sender' else 'DAP'

        commodities_block = ''
        customs_block = ''
        if is_international:
            commodities_block = self._build_commodities_xml(customs_items, currency_raw)
            customs_total = self._sum_customs_value(customs_items)
            customs_block = (
                f'<CustomsValue><Amount>{customs_total:.2f}</Amount>'
                f'<Currency>{currency}</Currency></CustomsValue>'
                f'<IncotermsCode>{escape(incoterms)}</IncotermsCode>'
                f'<ContentType>Merchandise</ContentType>'
            )

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

        # Adult-signature delivery confirmation (v0.7.0). ShipRush
        # uses the same <DCISType> element across all three carriers
        # but the accepted enum values differ: FedEx wants 'F4', UPS
        # and USPS want 'ADS'. Account key is the most reliable
        # signal for the FedEx branch since UPS and FedEx share a
        # numeric carrier_id of 1.
        dcis_block = ''
        if adult_signature:
            dcis_code = 'F4' if (account_key or '').upper() == 'FEDEX' else 'ADS'
            dcis_block = f'<DCISType>{dcis_code}</DCISType>'
            logger.info(
                "ShipRush: adult signature requested (carrier_account=%s, dcis=%s)",
                account_key, dcis_code,
            )

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
          <Country>{dest_country}</Country>
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
        {dcis_block}
      </Package>
      {customs_block}
      {commodities_block}
    </Shipment>
  </ShipTransaction>
  <ShipSettings>
    <PrinterShippingLabel><LabelType>ZPL</LabelType></PrinterShippingLabel>
    <AddressOption>3</AddressOption>
  </ShipSettings>
</ShipRequest>"""

        if is_international:
            logger.info(
                "ShipRush: international shipment (ref: %s, dest: %s, "
                "commodities: %d, currency: %s, incoterms: %s)",
                order_num, dest_country_raw, len(customs_items), currency_raw, incoterms,
            )
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

    # ---- international / customs XML -----------------------------------

    @staticmethod
    def _sum_customs_value(customs_items):
        """Total declared customs value across all line items.

        Safe to call on an empty or malformed list; missing fields
        contribute zero. ShipRush's <CustomsValue><Amount> takes the
        order-level total (not per-item) so this helper sums what the
        per-item rows declared.
        """
        total = 0.0
        for item in customs_items or []:
            try:
                qty = float(item.get('qty') or 0)
                unit = float(item.get('unit_value') or 0)
            except (TypeError, ValueError):
                continue
            if qty > 0 and unit > 0:
                total += qty * unit
        return total

    @staticmethod
    def _build_commodities_xml(customs_items, currency):
        """Build the <Commodities> block for an international shipment.

        Every string field is `escape`d to prevent XML injection from
        an upstream-supplied product description or HS code (Sentry
        is the trust boundary here but defense in depth is cheap).
        Numerics are formatted via `:f` so a malformed `qty` cannot
        break out of the tag. If an item is missing required fields
        we still emit a Commodity row -- ShipRush will reject the
        label, which is the correct fail-loud behavior on incomplete
        customs data rather than a silent partial declaration.
        """
        if not customs_items:
            return ''
        currency_safe = escape(str(currency or 'USD'))
        rows = []
        for item in customs_items:
            description = escape(str(item.get('description') or 'Merchandise'))
            hs_code = escape(str(item.get('hs_code') or ''))
            origin = escape(str(item.get('country_of_origin') or '').upper()[:2])
            try:
                qty = int(item.get('qty') or 0)
            except (TypeError, ValueError):
                qty = 0
            try:
                unit_weight_oz = float(item.get('unit_weight_oz') or 0)
            except (TypeError, ValueError):
                unit_weight_oz = 0.0
            try:
                unit_value = float(item.get('unit_value') or 0)
            except (TypeError, ValueError):
                unit_value = 0.0
            # ShipRush expects per-unit weight in pounds.
            unit_weight_lb = unit_weight_oz / 16.0 if unit_weight_oz else 0.0
            rows.append(
                f'    <Commodity>'
                f'<Description>{description}</Description>'
                f'<HarmonizedCode>{hs_code}</HarmonizedCode>'
                f'<CountryOfManufacture>{origin}</CountryOfManufacture>'
                f'<Quantity>{qty}</Quantity>'
                f'<UnitWeight>{unit_weight_lb:.4f}</UnitWeight>'
                f'<UnitValue><Amount>{unit_value:.2f}</Amount>'
                f'<Currency>{currency_safe}</Currency></UnitValue>'
                f'</Commodity>'
            )
        return '<Commodities>\n' + '\n'.join(rows) + '\n      </Commodities>'

    # ---- carrier/service resolution ------------------------------------

    # Fallback tuple when shiprush_services has no entry that matches.
    # The four values follow the existing convention: carrier_id '1'
    # (UPS), account_key 'UPS', service_code '03' (UPS Ground),
    # is_one_rate False. Callers always unpack a 4-tuple so this is
    # the safe shape under empty / under-configured settings.
    _RESOLVE_FALLBACK = ('1', 'UPS', '03', False)

    def _guard_fedex(self, method, resolved):
        """Refuse to answer a FedEx method with a non-FedEx account.

        _RESOLVE_FALLBACK is UPS Ground, so any unconfigured FedEx slot
        quietly produces a 1Z UPS label for an order that asked for FedEx.
        The operator sees "FedEx" on screen and the customer gets UPS, and
        nothing in the flow flags it. Log it loudly instead of shipping it
        silently; the caller still gets a usable tuple so a misconfigured
        install degrades rather than dies mid-pack.
        """
        account_key = (resolved[1] or '').upper()
        if account_key != 'FEDEX':
            logger.error(
                "Ship method %r asked for FedEx but resolved to the %s "
                "account (service %s). The matching shiprush_services slot "
                "is unconfigured. This will produce a %s label for a FedEx "
                "order.", method, account_key or 'fallback', resolved[2],
                account_key or 'UPS',
            )
        return resolved

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

        # A service code from a clicked rate row wins over every slot rule:
        # the operator picked a specific quoted service, and the quote also
        # told us which shipping account it belongs to.
        if isinstance(carrier_override, dict) and carrier_override.get('service_code'):
            return (
                str(carrier_override.get('carrier_id') or '1'),
                str(carrier_override.get('account_key') or ''),
                str(carrier_override['service_code']),
                bool(carrier_override.get('one_rate', False)),
            )

        if carrier_override == 'UPS':
            return _resolve_or_fallback('UPS_GROUND')
        if carrier_override == 'USPS':
            return _resolve_or_fallback('USPS_GROUND_ADV')
        if carrier_override == 'FEDEX_ONE_RATE_2DAY':
            return _resolve_or_fallback('FEDEX_ONE_RATE')

        method = (ns_method or '').lower()

        # FedEx first: a USPS *service* word can sit inside a FedEx
        # method name ("FedEx Priority Overnight", "FedEx First
        # Overnight"), so FedEx must be matched before the USPS gate
        # below -- otherwise the broadened USPS service-name match
        # would steal those orders.
        if 'fedex' in method:
            if '2day' in method or '2 day' in method or 'second day' in method:
                return self._guard_fedex(method, _resolve_or_fallback('FEDEX_2DAY', 'FEDEX_GROUND'))
            if 'overnight' in method:
                return self._guard_fedex(method, _resolve_or_fallback('FEDEX_OVERNIGHT', 'FEDEX_GROUND'))
            if 'one rate' in method or 'onerate' in method:
                return self._guard_fedex(method, _resolve_or_fallback('FEDEX_ONE_RATE', 'FEDEX_GROUND'))
            # Ground Economy (FSP, formerly SmartPost). Checked BEFORE the
            # plain-ground fallback because "FedEx Ground Economy" contains
            # "ground" and would otherwise land on FEDEX_GROUND -- and with
            # that slot unconfigured, on _RESOLVE_FALLBACK, which is UPS
            # Ground on the UPS account. That is a silent mis-ship: the
            # order says FedEx, the label says UPS. Same failure class as
            # stikman28/dockd#6.
            if 'economy' in method or 'smartpost' in method or 'smart post' in method:
                return self._guard_fedex(
                    method, _resolve_or_fallback('FEDEX_GROUND_ECONOMY', 'FEDEX_GROUND'))
            return self._guard_fedex(method, _resolve_or_fallback('FEDEX_GROUND'))

        # USPS, recognized by the carrier token OR a USPS service name.
        # The service-name set ('ground advantage' / 'priority' /
        # 'first class') mirrors CarrierEngine.current_carrier so a
        # method like "Priority Mail" -- which lacks the literal "usps"
        # token -- resolves to a USPS account instead of falling through
        # to the UPS Ground default. Before this, such orders shipped UPS
        # (a 1Z label on the UPS account) while dockd still reported them
        # as USPS, the carrier mislabel in stikman28/dockd#6. Keep this
        # list in lockstep with current_carrier's USPS branch.
        if ('usps' in method or 'post' in method or 'media' in method
                or 'ground advantage' in method or 'priority' in method
                or 'first class' in method):
            if 'priority' in method:
                return _resolve_or_fallback('USPS_PRIORITY', 'USPS_GROUND_ADV')
            if 'media' in method:
                return _resolve_or_fallback('USPS_MEDIA_MAIL', 'USPS_GROUND_ADV')
            if 'first' in method:
                return _resolve_or_fallback('USPS_FIRST_CLASS', 'USPS_GROUND_ADV')
            if 'parcel' in method:
                return _resolve_or_fallback('USPS_PARCEL', 'USPS_GROUND_ADV')
            return _resolve_or_fallback('USPS_GROUND_ADV', 'UPS_GROUND')

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
