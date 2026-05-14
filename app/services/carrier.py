"""Carrier optimization engine.

Pure logic class, zero Flask dependency. Determines optimal carrier
based on box dimensions, weight, rural surcharges, PO Box detection,
and FedEx One Rate box fitting.

Every tunable knob (box library, preferences, dim/weight thresholds,
carrier reason text, FedEx One Rate ordering) now lives in the
SettingsStore. CarrierEngine reads through to the store on each call,
so a settings PUT from the admin UI takes effect immediately for the
next ship.
"""

import csv
import logging

logger = logging.getLogger('dockd.carrier')


class CarrierEngine:

    def __init__(self, settings_store, rural_zip_path=None):
        self._settings = settings_store
        self.rural_zips = set()
        self.rural_zip_tiers = {}
        if rural_zip_path:
            self._load_rural_zips(rural_zip_path)

    # ---- settings projections -------------------------------------------

    def _box_map(self):
        return {b['id'].upper(): b for b in self._settings.get('boxes', [])}

    def _fedex_map(self):
        return {b['id'].upper(): b for b in self._settings.get('fedex_boxes', [])}

    def _fedex_one_rate_order(self):
        return list(self._settings.get('fedex_one_rate_order', []))

    def _rules(self):
        return self._settings.get('carrier_rules', {}) or {}

    def _reasons(self):
        return self._settings.get('carrier_reasons', {}) or {}

    def _carrier_methods(self):
        return self._settings.get('carrier_methods', {}) or {}

    @property
    def CARRIER_METHOD_IDS(self):
        """Back-compat read-through for callers that used the class attr."""
        return self._carrier_methods()

    def _preference_sets(self):
        boxes = self._settings.get('boxes', [])
        usps, ups, weight = set(), set(), set()
        for b in boxes:
            bid = b['id'].upper()
            pref = b.get('preference', 'USPS')
            if pref == 'USPS':
                usps.add(bid)
            elif pref == 'UPS':
                ups.add(bid)
            elif pref == 'WEIGHT_THRESHOLD':
                weight.add(bid)
        return usps, ups, weight

    @property
    def HIGH_VALUE_THRESHOLD(self):
        return float(self._settings.get('high_value_threshold', 200))

    # ---- rural zip surcharge data --------------------------------------

    def _load_rural_zips(self, path):
        try:
            with open(path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    zip_code = row['zip'].strip()[:5]
                    tier = row.get('tier', 'DAS').strip().upper()
                    self.rural_zips.add(zip_code)
                    self.rural_zip_tiers[zip_code] = tier
            logger.info("Loaded %d rural surcharge zip codes", len(self.rural_zips))
        except FileNotFoundError:
            logger.warning("Rural zip file not found at %s", path)

    # ---- carrier determination -----------------------------------------

    def determine_carrier(self, box_id, dims, weight):
        """Returns (carrier, reason). carrier is 'USPS', 'UPS', or None."""
        bid = str(box_id).upper()
        rules = self._rules()
        reasons = self._reasons()
        usps_boxes, ups_boxes, weight_boxes = self._preference_sets()

        weight_crossover = float(rules.get('weight_crossover_lb', 3.0))
        dim_warn = float(rules.get('dim_weight_warn', 10))
        dim_override = float(rules.get('dim_weight_override', 15))
        longest_side_max = float(rules.get('longest_side_max_in', 30))
        max_weight_usps = float(rules.get('max_weight_usps_lb', 15))

        if bid in ups_boxes:
            return 'UPS', reasons.get('UPS_BOX', '')

        if bid in usps_boxes:
            suggested, reason = 'USPS', reasons.get('USPS_BOX', '')
        elif bid in weight_boxes:
            if weight <= weight_crossover:
                suggested, reason = 'USPS', reasons.get('WEIGHT_USPS', '')
            else:
                suggested, reason = 'UPS', reasons.get('WEIGHT_UPS', '')
        else:
            dim_weight = (dims['l'] * dims['w'] * dims['h']) / 139
            longest_side = max(dims['l'], dims['w'], dims['h'])
            if dim_weight > dim_warn or longest_side > longest_side_max:
                return 'UPS', reasons.get('OB_UPS', '')
            suggested, reason = 'USPS', reasons.get('OB_USPS', '')

        # Dimensional override gate.
        if suggested == 'USPS':
            dim_weight = (dims['l'] * dims['w'] * dims['h']) / 139
            longest_side = max(dims['l'], dims['w'], dims['h'])
            if weight > max_weight_usps or dim_weight > dim_override or longest_side > longest_side_max:
                return 'UPS', reasons.get('DIM_OVERRIDE', '')

        return suggested, reason

    def current_carrier(self, ship_method):
        """Parse the current carrier from an order's ship-method label."""
        m = (ship_method or '').lower()
        if 'fedex' in m:
            return 'FEDEX'
        if 'ups' in m:
            return 'UPS'
        if 'usps' in m or 'ground advantage' in m or 'priority' in m \
                or 'first class' in m or 'media' in m:
            return 'USPS'
        return None

    def is_po_box(self, address):
        if not address:
            return False
        addr_str = str(address).upper()
        return 'PO BOX' in addr_str or 'P.O. BOX' in addr_str or 'P O BOX' in addr_str

    def is_rural(self, zip_code):
        return str(zip_code)[:5] in self.rural_zips

    def get_rural_tier(self, zip_code):
        return self.rural_zip_tiers.get(str(zip_code)[:5], 'DAS')

    def resolve_box_dims(self, box_id, ship_method, ob_dims=None):
        """Resolve effective box ID, dimensions, and packaging code."""
        bid = str(box_id).upper() if box_id else ''
        packaging_code = '02'
        dims = {'l': 12, 'w': 10, 'h': 6}
        box_map = self._box_map()
        fedex_map = self._fedex_map()

        if bid == 'OB' and ob_dims:
            dims = {
                'l': float(ob_dims.get('l', 12)),
                'w': float(ob_dims.get('w', 10)),
                'h': float(ob_dims.get('h', 6)),
            }
            return bid, dims, packaging_code

        method_lower = ship_method.lower() if ship_method else ''

        if 'fedex' in method_lower:
            if bid in ('FDXENV', 'FXENV') and 'FDXENV' in fedex_map:
                f = fedex_map['FDXENV']
                return 'FDXENV', {'l': f['l'], 'w': f['w'], 'h': f['h']}, f['type']
            if bid in fedex_map:
                f = fedex_map[bid]
                return bid, {'l': f['l'], 'w': f['w'], 'h': f['h']}, f['type']
            if bid in box_map:
                scanned = box_map[bid]
                return self.best_fedex_one_rate_box(scanned['l'], scanned['w'], scanned['h'])
            return bid, dims, packaging_code

        if bid in fedex_map:
            f = fedex_map[bid]
            return bid, {'l': f['l'], 'w': f['w'], 'h': f['h']}, f['type']
        if bid in box_map:
            d = box_map[bid]
            dims = {'l': d['l'], 'w': d['w'], 'h': d['h']}
        return bid, dims, packaging_code

    @staticmethod
    def _box_fits(scanned_l, scanned_w, scanned_h, box_l, box_w, box_h):
        s = sorted([float(scanned_l), float(scanned_w), float(scanned_h)])
        b = sorted([float(box_l), float(box_w), float(box_h)])
        return s[0] <= b[0] and s[1] <= b[1] and s[2] <= b[2]

    def best_fedex_one_rate_box(self, l, w, h):
        """Find the smallest FedEx One Rate box that fits the given dims."""
        fedex_map = self._fedex_map()
        order = self._fedex_one_rate_order()
        for fid in order:
            fid_up = fid.upper()
            if fid_up not in fedex_map:
                continue
            d = fedex_map[fid_up]
            if self._box_fits(l, w, h, d['l'], d['w'], d['h']):
                return fid_up, {'l': d['l'], 'w': d['w'], 'h': d['h']}, d['type']
        if order:
            fid_up = order[-1].upper()
            d = fedex_map.get(fid_up)
            if d:
                return fid_up, {'l': d['l'], 'w': d['w'], 'h': d['h']}, d['type']
        return None, {'l': l, 'w': w, 'h': h}, '02'

    def check_carrier_conflict(self, box_id, dims, weight, ship_method,
                               dest_zip, address, ca_shipping_paid,
                               dest_country='US'):
        """Check if the optimal carrier differs from the order's current
        carrier. Returns a conflict dict if a switch should be suggested,
        or None."""
        method_lower = (ship_method or '').lower()
        rules = self._rules()
        reasons = self._reasons()
        usps_boxes, _, _ = self._preference_sets()

        rural_ca_max = float(rules.get('rural_ca_shipping_max', 5.0))
        usps_to_ups_ca_min = float(rules.get('usps_to_ups_ca_shipping_min', 5.0))

        # Skip FedEx orders entirely.
        if 'fedex' in method_lower:
            return None

        # International (v0.7.0): the rural-ZIP, PO Box, and USPS-vs-UPS
        # swap heuristics are all keyed on US carrier physics. For
        # non-US destinations the legacy carrier picked upstream
        # (Sentry / the order entry system) is authoritative; dockd
        # does not second-guess. ShipRush still gets the label, but
        # this conflict-suggestion modal stays out of the way.
        normalized_country = str(dest_country or 'US').strip().upper()[:2] or 'US'
        if normalized_country != 'US':
            return None

        optimal, reason = self.determine_carrier(box_id, dims, weight)
        current = self.current_carrier(ship_method)

        dest_zip = str(dest_zip or '')[:5]
        is_rural = self.is_rural(dest_zip)
        is_po = self.is_po_box(address)

        # Rural zip + UPS order + USPS-eligible box -> suggest USPS to avoid DAS.
        if is_rural and current == 'UPS' and ca_shipping_paid <= rural_ca_max:
            if optimal == 'USPS' or str(box_id).upper() in usps_boxes:
                tier = self.get_rural_tier(dest_zip)
                logger.info("Rural surcharge detected", extra={
                    'dest_zip': dest_zip, 'tier': tier, 'box_id': box_id,
                    'suggested': 'USPS', 'ca_shipping_paid': ca_shipping_paid,
                })
                rural_reason = reasons.get(
                    'RURAL_DAS',
                    'Rural surcharge area ({tier})',
                ).replace('{tier}', tier)
                return {
                    'status': 'carrier_conflict',
                    'suggested': 'USPS',
                    'current': ship_method,
                    'current_carrier': 'UPS',
                    'reason': rural_reason,
                    'box_id': box_id,
                }

        if optimal and current and optimal != current:
            # Don't suggest UPS->USPS if customer paid more than the
            # configured shipping threshold.
            if optimal == 'USPS' and current == 'UPS' and ca_shipping_paid > usps_to_ups_ca_min:
                logger.info("Skipping UPS->USPS: customer paid $%.2f shipping", ca_shipping_paid)
                return None
            # Don't suggest USPS->UPS if rural (keep USPS to avoid DAS)
            # UNLESS package is dimensionally too big for USPS.
            if (optimal == 'UPS' and current == 'USPS' and is_rural
                    and reason != reasons.get('DIM_OVERRIDE')):
                logger.info("Skipping USPS->UPS: rural zip %s", dest_zip)
                return None
            # Don't suggest USPS->UPS if PO Box.
            if optimal == 'UPS' and current == 'USPS' and is_po:
                logger.info("Skipping USPS->UPS: PO Box address")
                return None

            logger.info("Carrier conflict: current=%s, suggested=%s, reason=%s",
                        current, optimal, reason)
            return {
                'status': 'carrier_conflict',
                'suggested': optimal,
                'current': ship_method,
                'current_carrier': current,
                'reason': reason,
                'box_id': box_id,
            }

        return None
