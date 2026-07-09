"""Tests for the Postgres-backed SettingsStore (Phase 2c)."""

import pytest

from app.services.default_settings import DEFAULT_SETTINGS


@pytest.fixture
def store(app):
    """The app's settings_store; conftest seeds DEFAULT_SETTINGS per test."""
    return app.settings_store


class TestSettingsStore:

    def test_bootstrap_from_defaults(self, store):
        assert store.get('high_value_threshold') == DEFAULT_SETTINGS['high_value_threshold']
        # A nested section round-trips through JSONB as a native dict.
        assert store.get('carrier_rules') == DEFAULT_SETTINGS['carrier_rules']

    def test_patch_partial_update(self, store):
        original_boxes = store.get('boxes')
        store.patch({'high_value_threshold': 350})
        assert store.get('high_value_threshold') == 350
        assert store.get('boxes') == original_boxes

    def test_replace_merges_with_defaults(self, store):
        # A body that omits boxes must not blow away the box library.
        store.replace({'high_value_threshold': 99})
        assert store.get('high_value_threshold') == 99
        assert len(store.get('boxes', [])) == len(DEFAULT_SETTINGS['boxes'])

    def test_get_unknown_key_falls_back_to_default(self, store):
        assert store.get('does_not_exist', 'fallback') == 'fallback'

    def test_write_is_visible_immediately(self, store):
        # No mtime cache: a patch is visible on the very next read.
        store.patch({'high_value_threshold': 1234})
        assert store.all()['high_value_threshold'] == 1234

    def test_public_subset_excludes_internals(self, store):
        public = store.public_subset()
        assert 'high_value_threshold' in public
        assert 'amazon_methods' in public
        assert 'boxes' in public
        assert 'shiprush_accounts' not in public
        assert 'stations' not in public
        assert 'carrier_methods' not in public

    def test_secret_inline_but_hidden_from_public(self, store):
        # Secrets live inline in dockd_settings; admin all() sees them,
        # public_subset() must not.
        store.patch({'shiprush_accounts': {'acct1': 'GUID-123'}})
        assert store.all()['shiprush_accounts'] == {'acct1': 'GUID-123'}
        assert 'shiprush_accounts' not in store.public_subset()

    def test_is_country_banned(self, store):
        # Default banned list seeds OFAC comprehensive-sanctions (CU/IR/KP/SY).
        assert store.is_country_banned('IR') is True
        assert store.is_country_banned('ir') is True
        assert store.is_country_banned('US') is False
        assert store.is_country_banned(None) is False


class TestSettingsRequestCache:
    """A single ship reads settings ~14 times; those reads share one query
    per request via a flask.g snapshot, without leaking across requests."""

    def test_all_is_cached_within_a_request(self, app, store):
        with app.test_request_context():
            first = store.all()
            second = store.all()
            # Same object -> the second read hit the g cache, not the DB.
            assert first is second

    def test_get_shares_the_request_cache(self, app, store):
        with app.test_request_context():
            snapshot = store.all()
            # get() serves from the same cached snapshot.
            assert store.get('high_value_threshold') is snapshot['high_value_threshold']

    def test_cache_does_not_leak_across_requests(self, app, store):
        with app.test_request_context():
            a = store.all()
        with app.test_request_context():
            b = store.all()
        assert a is not b

    def test_no_cache_outside_request_context(self, store):
        # No request -> no g to cache on -> every call queries fresh.
        assert store.all() is not store.all()

    def test_write_invalidates_the_request_cache(self, app, store):
        with app.test_request_context():
            store.all()  # prime the cache
            store.patch({'high_value_threshold': 4242})
            # Post-write read in the same request reflects the write.
            assert store.all()['high_value_threshold'] == 4242
