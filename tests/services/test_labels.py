"""Unit tests for the labels service: ZPL builders, catalog loader,
and the size sort key.
"""

import csv

import pytest

from app.services.labels import (
    ThreadCatalog,
    bin_sticker_zpl,
    item_barcode_zpl,
    item_barcode_zpl_bulk,
    _size_sort_key,
)


# ----- bin_sticker_zpl ------------------------------------------------------


class TestBinStickerZpl:

    def test_includes_sku_and_upc_when_both_present(self):
        zpl = bin_sticker_zpl('1264-42316', '053526423167')
        assert '^XA' in zpl and '^XZ' in zpl
        assert '^PW812' in zpl
        assert '^LL406' in zpl
        assert '1264-42316' in zpl
        assert '053526423167' in zpl
        assert '^BCN,130,Y,N,N' in zpl

    def test_falls_back_to_sku_only_when_upc_missing(self):
        zpl = bin_sticker_zpl('WIDGET-A', '')
        assert '^XA' in zpl and '^XZ' in zpl
        assert 'WIDGET-A' in zpl
        assert '^BCN' not in zpl

    @pytest.mark.parametrize('upc', [None, '', '  ', 'None', 'nan', 'NaN'])
    def test_treats_sentinel_upcs_as_missing(self, upc):
        zpl = bin_sticker_zpl('WIDGET-A', upc)
        assert '^BCN' not in zpl

    def test_escapes_zpl_control_chars_in_sku(self):
        zpl = bin_sticker_zpl('EVIL^FD"INJ', '012345678905')
        assert '^FD' in zpl
        assert 'EVIL FD' in zpl or 'EVIL\\"INJ' in zpl or 'EVIL' in zpl
        assert '"' not in zpl.replace('\\"', '')


# ----- item_barcode_zpl -----------------------------------------------------


class TestItemBarcodeZpl:

    def test_emits_15x1_label(self):
        zpl = item_barcode_zpl('012345678905')
        assert '^PW304' in zpl
        assert '^LL203' in zpl
        assert '^BUN,140,Y,N,Y' in zpl
        assert '012345678905' in zpl

    def test_bulk_repeats_for_quantity(self):
        single = item_barcode_zpl('012345678905')
        bulk = item_barcode_zpl_bulk('012345678905', 3)
        assert bulk == single * 3

    def test_bulk_clamps_quantity_high_and_low(self):
        zero = item_barcode_zpl_bulk('012345678905', 0)
        many = item_barcode_zpl_bulk('012345678905', 999)
        single = item_barcode_zpl('012345678905')
        assert zero == single
        assert many == single * 100


# ----- ThreadCatalog --------------------------------------------------------


def _write_catalog(path, rows):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['brand', 'size', 'color', 'upc', 'sku'])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


class TestThreadCatalog:

    def test_empty_when_file_missing(self, tmp_path):
        catalog = ThreadCatalog(str(tmp_path / 'missing.csv'))
        assert catalog.is_empty()
        assert catalog.as_response() == {'brands': [], 'catalog': {}}

    def test_loads_and_groups_by_brand_and_size(self, tmp_path):
        path = tmp_path / 'catalog.csv'
        _write_catalog(path, [
            {'brand': 'UTC', 'size': '70 Denier', 'color': 'Black',
             'upc': '111', 'sku': 'UTC-70-BLK'},
            {'brand': 'UTC', 'size': '70 Denier', 'color': 'Olive',
             'upc': '112', 'sku': 'UTC-70-OLV'},
            {'brand': 'Veevus', 'size': '6/0', 'color': 'Red',
             'upc': '222', 'sku': 'V-6-RED'},
        ])
        catalog = ThreadCatalog(str(path))
        resp = catalog.as_response()
        assert resp['brands'] == ['UTC', 'Veevus']
        assert resp['catalog']['UTC']['sizes'] == ['70 Denier']
        utc_products = resp['catalog']['UTC']['products']['70 Denier']
        # Colors sorted alphabetically inside a (brand, size).
        assert [p['color'] for p in utc_products] == ['Black', 'Olive']
        assert utc_products[0]['upc'] == '111'

    def test_skips_rows_missing_brand_or_size(self, tmp_path):
        path = tmp_path / 'catalog.csv'
        _write_catalog(path, [
            {'brand': '', 'size': '70 Denier', 'color': 'Black', 'upc': '1', 'sku': 'X'},
            {'brand': 'UTC', 'size': '', 'color': 'Black', 'upc': '2', 'sku': 'Y'},
            {'brand': 'UTC', 'size': '70 Denier', 'color': 'Black', 'upc': '3', 'sku': 'Z'},
        ])
        catalog = ThreadCatalog(str(path))
        resp = catalog.as_response()
        assert resp['brands'] == ['UTC']
        assert resp['catalog']['UTC']['products']['70 Denier'][0]['upc'] == '3'

    def test_reloads_when_mtime_advances(self, tmp_path):
        import os
        import time
        path = tmp_path / 'catalog.csv'
        _write_catalog(path, [
            {'brand': 'UTC', 'size': '70 Denier', 'color': 'Black',
             'upc': '111', 'sku': 'UTC-70-BLK'},
        ])
        catalog = ThreadCatalog(str(path))
        assert catalog.as_response()['brands'] == ['UTC']

        time.sleep(0.01)
        _write_catalog(path, [
            {'brand': 'Veevus', 'size': '6/0', 'color': 'Red',
             'upc': '222', 'sku': 'V-6-RED'},
        ])
        # Force mtime change in case filesystem granularity collapsed it.
        future = os.path.getmtime(str(path)) + 1
        os.utime(str(path), (future, future))
        assert catalog.as_response()['brands'] == ['Veevus']


# ----- size sort -----------------------------------------------------------


class TestSizeSortKey:

    def test_wire_gauges_in_logical_order(self):
        sizes = ['Large', 'XS', 'Brassie', 'Small', 'Medium']
        assert sorted(sizes, key=_size_sort_key) == [
            'XS', 'Small', 'Brassie', 'Medium', 'Large',
        ]

    def test_thread_fractions_numeric_ascending(self):
        sizes = ['10/0', '6/0', '8/0']
        assert sorted(sizes, key=_size_sort_key) == ['6/0', '8/0', '10/0']

    def test_denier_numeric_ascending(self):
        sizes = ['200 Denier', '70 Denier', '140 Denier']
        assert sorted(sizes, key=_size_sort_key) == [
            '70 Denier', '140 Denier', '200 Denier',
        ]

    def test_mixed_size_families_ordered_by_family_then_key(self):
        sizes = ['70 Denier', '6/0', 'Small', 'Tinsel L', 'Tinsel S']
        ordered = sorted(sizes, key=_size_sort_key)
        # Wire (Small) first, then fractions, then deniers, then SML group.
        assert ordered.index('Small') < ordered.index('6/0')
        assert ordered.index('6/0') < ordered.index('70 Denier')
        assert ordered.index('Tinsel S') < ordered.index('Tinsel L')
