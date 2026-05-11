"""Tests for LabelCache."""

import os
import base64
import pytest
from app.services.label_cache import LabelCache


@pytest.fixture
def cache(tmp_path):
    return LabelCache(
        history_file=str(tmp_path / 'history.json'),
        label_dir=str(tmp_path / 'labels'),
        max_age_hours=8,
    )


def test_save_and_lookup(cache):
    zpl_b64 = base64.b64encode(b'^XA TEST ^XZ').decode()
    cache.save('12345', 'SH-99', '1Z999', zpl_b64)

    record = cache.lookup('12345')
    assert record is not None
    assert record['tracking'] == '1Z999'
    assert record['shipment_id'] == 'SH-99'


def test_lookup_by_tracking(cache):
    zpl_b64 = base64.b64encode(b'^XA TEST ^XZ').decode()
    cache.save('12345', 'SH-99', '1Z999', zpl_b64)

    record = cache.lookup('1Z999')
    assert record is not None
    assert record['tracking'] == '1Z999'


def test_lookup_by_so_prefix(cache):
    zpl_b64 = base64.b64encode(b'^XA TEST ^XZ').decode()
    cache.save('12345', 'SH-99', '1Z999', zpl_b64)

    record = cache.lookup('SO12345')
    assert record is not None


def test_get_zpl_b64(cache):
    original = base64.b64encode(b'^XA TEST ^XZ').decode()
    cache.save('12345', 'SH-99', '1Z999', original)

    result = cache.get_zpl_b64('12345')
    assert result is not None
    decoded = base64.b64decode(result)
    assert decoded == b'^XA TEST ^XZ'


def test_get_shipment_id(cache):
    zpl_b64 = base64.b64encode(b'^XA TEST ^XZ').decode()
    cache.save('12345', 'SH-99', '1Z999', zpl_b64)

    assert cache.get_shipment_id('12345') == 'SH-99'


def test_lookup_not_found(cache):
    assert cache.lookup('nonexistent') is None


def test_get_zpl_not_found(cache):
    assert cache.get_zpl_b64('nonexistent') is None
