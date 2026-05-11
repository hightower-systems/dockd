"""Tests for CarrierEngine - pure logic, no Flask dependency.

The engine reads from a settings store. Tests provide a fixed example
box library so they remain stable as the shipping repo's defaults
evolve (open-source defaults are empty; tests need a concrete library
to exercise the determine_carrier / conflict / fedex logic).
"""

import pytest
from app.services.carrier import CarrierEngine


# ----- example box library used by the test suite ------------------------

_EXAMPLE_BOXES = [
    {"id": "1",  "label": "Box 1",  "l": 8,  "w": 4,  "h": 4,   "preference": "USPS"},
    {"id": "2",  "label": "Box 2",  "l": 6,  "w": 6,  "h": 6,   "preference": "USPS"},
    {"id": "8",  "label": "Box 8",  "l": 14, "w": 8,  "h": 8,   "preference": "USPS"},
    {"id": "12", "label": "Box 12", "l": 26, "w": 15, "h": 5,   "preference": "WEIGHT_THRESHOLD"},
    {"id": "14", "label": "Box 14", "l": 32, "w": 10, "h": 6.5, "preference": "UPS"},
    {"id": "15", "label": "Box 15", "l": 16, "w": 14, "h": 8,   "preference": "WEIGHT_THRESHOLD"},
    {"id": "16", "label": "Box 16", "l": 18, "w": 14, "h": 10,  "preference": "UPS"},
    {"id": "17", "label": "Box 17", "l": 15, "w": 15, "h": 6,   "preference": "WEIGHT_THRESHOLD"},
    {"id": "SMALLBUB", "label": "Small Bubble", "l": 9,  "w": 7, "h": 0.5, "preference": "USPS"},
]

_EXAMPLE_FEDEX = [
    {"id": "FDXENV",   "label": "FedEx Envelope",  "l": 12, "w": 9,  "h": 1,  "type": "01"},
    {"id": "FDXPAK",   "label": "FedEx Pak",       "l": 15, "w": 12, "h": 1,  "type": "04"},
    {"id": "FDXSMALL", "label": "FedEx Small Box", "l": 12, "w": 10, "h": 2,  "type": "2A"},
    {"id": "FDXMED",   "label": "FedEx Medium Box","l": 13, "w": 11, "h": 2,  "type": "2B"},
    {"id": "FDXLARGE", "label": "FedEx Large Box", "l": 17, "w": 12, "h": 3,  "type": "2C"},
    {"id": "FDXXL",    "label": "FedEx XL Box",    "l": 11, "w": 11, "h": 11, "type": "EBEL"},
]

_EXAMPLE_FEDEX_ORDER = ["FDXENV", "FDXPAK", "FDXSMALL", "FDXMED", "FDXLARGE", "FDXXL"]

_EXAMPLE_RULES = {
    "weight_crossover_lb": 3.0,
    "dim_weight_warn": 10,
    "dim_weight_override": 15,
    "longest_side_max_in": 30,
    "max_weight_usps_lb": 15,
    "rural_ca_shipping_max": 5.0,
    "usps_to_ups_ca_shipping_min": 5.0,
}

_EXAMPLE_REASONS = {
    "USPS_BOX":     "Small box - USPS is cheaper",
    "UPS_BOX":      "Large/oversized box - UPS is cheaper",
    "WEIGHT_USPS":  "Under 3 lb - USPS is cheaper for this box size",
    "WEIGHT_UPS":   "Over 3 lb - UPS is cheaper for this box size",
    "OB_USPS":      "Custom box dims favor USPS",
    "OB_UPS":       "Custom box dims favor UPS (high dim-weight or long side)",
    "DIM_OVERRIDE": "Package exceeds USPS size/weight limits - UPS recommended",
    "RURAL_DAS":    "Rural surcharge area ({tier}) - USPS has no area surcharge",
}


class _FakeSettings:
    """Read-only settings shim used by the engine in unit tests."""

    def __init__(self, overrides=None):
        self._data = {
            "boxes": _EXAMPLE_BOXES,
            "fedex_boxes": _EXAMPLE_FEDEX,
            "fedex_one_rate_order": _EXAMPLE_FEDEX_ORDER,
            "carrier_rules": _EXAMPLE_RULES,
            "carrier_reasons": _EXAMPLE_REASONS,
            "carrier_methods": {},
            "high_value_threshold": 200,
        }
        if overrides:
            self._data.update(overrides)

    def get(self, key, default=None):
        return self._data.get(key, default)


def _preference_sets(settings):
    usps, ups, weight = set(), set(), set()
    for b in settings.get('boxes', []):
        bid = b['id'].upper()
        pref = b.get('preference', 'USPS')
        if pref == 'USPS':
            usps.add(bid)
        elif pref == 'UPS':
            ups.add(bid)
        elif pref == 'WEIGHT_THRESHOLD':
            weight.add(bid)
    return usps, ups, weight


@pytest.fixture
def engine():
    return CarrierEngine(_FakeSettings())


@pytest.fixture
def preference_sets():
    return _preference_sets(_FakeSettings())


@pytest.fixture
def fedex_order():
    return list(_EXAMPLE_FEDEX_ORDER)


class TestDetermineCarrier:

    def test_usps_boxes(self, engine, preference_sets):
        usps_boxes, _, _ = preference_sets
        for box_id in usps_boxes:
            carrier, _ = engine.determine_carrier(box_id, {'l': 8, 'w': 6, 'h': 6}, 1.0)
            assert carrier == 'USPS', f"Box {box_id} should be USPS"

    def test_ups_boxes(self, engine, preference_sets):
        _, ups_boxes, _ = preference_sets
        for box_id in ups_boxes:
            carrier, _ = engine.determine_carrier(box_id, {'l': 18, 'w': 14, 'h': 10}, 5.0)
            assert carrier == 'UPS', f"Box {box_id} should be UPS"

    def test_weight_threshold_light(self, engine, preference_sets):
        _, _, weight_boxes = preference_sets
        for box_id in weight_boxes:
            carrier, _ = engine.determine_carrier(box_id, {'l': 16, 'w': 14, 'h': 8}, 2.5)
            assert carrier == 'USPS'

    def test_weight_threshold_heavy(self, engine, preference_sets):
        _, _, weight_boxes = preference_sets
        for box_id in weight_boxes:
            carrier, _ = engine.determine_carrier(box_id, {'l': 16, 'w': 14, 'h': 8}, 4.0)
            assert carrier == 'UPS'

    def test_weight_threshold_boundary(self, engine):
        carrier, _ = engine.determine_carrier('12', {'l': 26, 'w': 15, 'h': 5}, 3.0)
        assert carrier == 'USPS'

    def test_ob_small_dims(self, engine):
        carrier, _ = engine.determine_carrier('OB', {'l': 10, 'w': 8, 'h': 6}, 2.0)
        assert carrier == 'USPS'

    def test_ob_large_dims(self, engine):
        carrier, _ = engine.determine_carrier('OB', {'l': 30, 'w': 20, 'h': 15}, 8.0)
        assert carrier == 'UPS'

    def test_ob_long_side(self, engine):
        carrier, _ = engine.determine_carrier('OB', {'l': 35, 'w': 5, 'h': 5}, 2.0)
        assert carrier == 'UPS'

    def test_dim_override_heavy_usps_box(self, engine):
        carrier, reason = engine.determine_carrier('1', {'l': 8, 'w': 4, 'h': 4}, 16.0)
        assert carrier == 'UPS'
        assert 'exceeds' in reason.lower() or 'limits' in reason.lower()

    def test_case_insensitive_box_id(self, engine):
        carrier1, _ = engine.determine_carrier('14', {'l': 32, 'w': 10, 'h': 6.5}, 5.0)
        carrier2, _ = engine.determine_carrier('SMALLBUB', {'l': 9, 'w': 7, 'h': 0.5}, 0.5)
        assert carrier1 == 'UPS'
        assert carrier2 == 'USPS'

    def test_no_box_overlap(self, preference_sets):
        usps_boxes, ups_boxes, weight_boxes = preference_sets
        assert not (usps_boxes & ups_boxes)
        assert not (usps_boxes & weight_boxes)


class TestCurrentCarrier:

    def test_ups(self, engine):
        assert engine.current_carrier('UPS - Ground') == 'UPS'

    def test_usps(self, engine):
        assert engine.current_carrier('USPS Ground Advantage') == 'USPS'

    def test_fedex(self, engine):
        assert engine.current_carrier('FedEx One Rate 2 Day') == 'FEDEX'

    def test_priority_mail(self, engine):
        assert engine.current_carrier('Priority Mail') == 'USPS'

    def test_unknown(self, engine):
        assert engine.current_carrier('Some Unknown Method') is None


class TestPOBox:

    def test_po_box(self, engine):
        assert engine.is_po_box('PO BOX 123') is True

    def test_po_box_dotted(self, engine):
        assert engine.is_po_box('P.O. BOX 456') is True

    def test_po_box_spaced(self, engine):
        assert engine.is_po_box('P O BOX 789') is True

    def test_not_po_box(self, engine):
        assert engine.is_po_box('123 Main Street') is False

    def test_none(self, engine):
        assert engine.is_po_box(None) is False


class TestFedExOneRate:

    def test_small_fits_envelope(self, engine):
        fid, _, _ = engine.best_fedex_one_rate_box(10, 8, 0.5)
        assert fid == 'FDXENV'

    def test_medium_fits_pak(self, engine):
        fid, _, _ = engine.best_fedex_one_rate_box(14, 11, 1)
        assert fid == 'FDXPAK'

    def test_oversized_falls_to_xl(self, engine):
        fid, _, _ = engine.best_fedex_one_rate_box(50, 50, 50)
        assert fid == 'FDXXL'


class TestBoxFits:

    def test_exact_fit(self):
        assert CarrierEngine._box_fits(12, 9, 1, 12, 9, 1) is True

    def test_too_big(self):
        assert CarrierEngine._box_fits(15, 12, 2, 12, 9, 1) is False

    def test_rotated_fit(self):
        assert CarrierEngine._box_fits(9, 1, 12, 12, 9, 1) is True


class TestCarrierConflict:

    def test_no_conflict_when_matching(self, engine):
        result = engine.check_carrier_conflict(
            '1', {'l': 8, 'w': 4, 'h': 4}, 1.0,
            'USPS Ground Advantage', '80112', '123 Main St', 0,
        )
        assert result is None

    def test_conflict_when_mismatched(self, engine):
        result = engine.check_carrier_conflict(
            '14', {'l': 32, 'w': 10, 'h': 6.5}, 5.0,
            'USPS Ground Advantage', '80112', '123 Main St', 0,
        )
        assert result is not None
        assert result['suggested'] == 'UPS'

    def test_skip_fedex(self, engine):
        result = engine.check_carrier_conflict(
            '1', {'l': 8, 'w': 4, 'h': 4}, 1.0,
            'FedEx 2 Day', '80112', '123 Main St', 0,
        )
        assert result is None

    def test_skip_ups_to_usps_high_shipping(self, engine):
        result = engine.check_carrier_conflict(
            '1', {'l': 8, 'w': 4, 'h': 4}, 1.0,
            'UPS - Ground', '80112', '123 Main St', 10.0,
        )
        assert result is None

    def test_skip_usps_to_ups_po_box(self, engine):
        result = engine.check_carrier_conflict(
            '14', {'l': 32, 'w': 10, 'h': 6.5}, 5.0,
            'USPS Ground Advantage', '80112', 'PO BOX 123', 0,
        )
        assert result is None


class TestResolveBoxDims:

    def test_known_box(self, engine):
        _, dims, _ = engine.resolve_box_dims('1', 'USPS Ground Advantage')
        assert dims['l'] == 8
        assert dims['w'] == 4

    def test_ob_custom_dims(self, engine):
        eid, dims, _ = engine.resolve_box_dims('OB', 'UPS - Ground', {'l': 20, 'w': 15, 'h': 10})
        assert dims['l'] == 20.0
        assert eid == 'OB'

    def test_fedex_method_with_regular_box(self, engine, fedex_order):
        eid, _, _ = engine.resolve_box_dims('1', 'FedEx 2 Day')
        assert eid in fedex_order

    def test_fedex_envelope(self, engine):
        eid, _, pkg = engine.resolve_box_dims('FDXENV', 'FedEx 2 Day')
        assert eid == 'FDXENV'
        assert pkg == '01'
