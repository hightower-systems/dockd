"""Tests for SettingsStore and UsersStore."""

import json
import os
import stat
import tempfile

import pytest

from app.services.default_settings import DEFAULT_SETTINGS
from app.services.settings import SettingsStore
from app.services.users_store import UsersStore, UsersStoreError


@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ---------- SettingsStore -------------------------------------------------


class TestSettingsStore:

    def test_bootstrap_from_defaults(self, tmp_path):
        path = os.path.join(tmp_path, 'settings.json')
        store = SettingsStore(path)
        assert os.path.exists(path)
        data = store.all()
        assert data['high_value_threshold'] == DEFAULT_SETTINGS['high_value_threshold']

    def test_file_is_600(self, tmp_path):
        path = os.path.join(tmp_path, 'settings.json')
        SettingsStore(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_patch_partial_update(self, tmp_path):
        path = os.path.join(tmp_path, 'settings.json')
        store = SettingsStore(path)
        original_boxes = store.get('boxes')
        store.patch({'high_value_threshold': 350})
        assert store.get('high_value_threshold') == 350
        assert store.get('boxes') == original_boxes

    def test_replace_merges_with_defaults(self, tmp_path):
        path = os.path.join(tmp_path, 'settings.json')
        store = SettingsStore(path)
        # Send a body that omits boxes; replace should pull them back in
        # from defaults so a UI bug cannot blow away the box library.
        store.replace({'high_value_threshold': 99})
        assert store.get('high_value_threshold') == 99
        assert len(store.get('boxes', [])) == len(DEFAULT_SETTINGS['boxes'])

    def test_cache_invalidated_on_external_write(self, tmp_path):
        path = os.path.join(tmp_path, 'settings.json')
        store = SettingsStore(path)
        store.get('high_value_threshold')  # warm cache
        # Mutate the file behind the store's back.
        data = json.loads(open(path).read())
        data['high_value_threshold'] = 777
        with open(path, 'w') as f:
            json.dump(data, f)
        # mtime change should bust the cache on next access.
        os.utime(path, None)
        assert store.get('high_value_threshold') == 777

    def test_public_subset_excludes_internals(self, tmp_path):
        path = os.path.join(tmp_path, 'settings.json')
        store = SettingsStore(path)
        public = store.public_subset()
        assert 'high_value_threshold' in public
        assert 'amazon_methods' in public
        assert 'boxes' in public
        assert 'shiprush_accounts' not in public
        assert 'stations' not in public
        assert 'carrier_methods' not in public


# ---------- UsersStore ----------------------------------------------------


class TestUsersStore:

    def test_bootstrap_with_admin(self, tmp_path):
        path = os.path.join(tmp_path, 'users.json')
        store = UsersStore(path)
        users = store.list_users()
        admin_record = next(u for u in users if u['username'] == 'admin')
        assert admin_record['role'] == 'admin'
        assert admin_record['must_change_password'] is True

    def test_bootstrap_password_is_admin(self, tmp_path):
        path = os.path.join(tmp_path, 'users.json')
        store = UsersStore(path)
        assert store.verify('admin', 'admin') is not None
        assert store.verify('admin', 'wrong-pw') is None

    def test_change_own_password_clears_must_change(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        store.change_own_password('admin', 'admin', 'newpass!')
        result = store.verify('admin', 'newpass!')
        assert result['must_change_password'] is False

    def test_change_own_password_requires_current(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        with pytest.raises(UsersStoreError):
            store.change_own_password('admin', 'wrong', 'newpass!')

    def test_change_own_password_rejects_same(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        with pytest.raises(UsersStoreError):
            store.change_own_password('admin', 'admin', 'admin')

    def test_file_is_600(self, tmp_path):
        path = os.path.join(tmp_path, 'users.json')
        UsersStore(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_add_user(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        store.add_user('mike', 'pw1234', 'user')
        usernames = [u['username'] for u in store.list_users()]
        assert 'mike' in usernames

    def test_add_user_rejects_short_password(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        with pytest.raises(UsersStoreError):
            store.add_user('mike', 'no', 'user')

    def test_add_user_rejects_duplicate(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        store.add_user('mike', 'pw1234', 'user')
        with pytest.raises(UsersStoreError):
            store.add_user('mike', 'pw5678', 'user')

    def test_add_user_rejects_invalid_role(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        with pytest.raises(UsersStoreError):
            store.add_user('mike', 'pw1234', 'superuser')

    def test_verify_password(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        store.add_user('mike', 'pw1234', 'user')
        result = store.verify('mike', 'pw1234')
        assert result['username'] == 'mike'
        assert result['role'] == 'user'
        # New users default to must_change_password=True.
        assert result['must_change_password'] is True
        assert store.verify('mike', 'wrong') is None
        assert store.verify('ghost', 'pw1234') is None

    def test_cannot_remove_last_admin(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        with pytest.raises(UsersStoreError):
            store.remove_user('admin')

    def test_remove_non_last_admin_works(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        store.add_user('other_admin', 'pw1234', 'admin')
        store.remove_user('other_admin')
        usernames = [u['username'] for u in store.list_users()]
        assert 'other_admin' not in usernames

    def test_cannot_demote_only_admin(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        with pytest.raises(UsersStoreError):
            store.set_role('admin', 'user')

    def test_set_password(self, tmp_path):
        store = UsersStore(os.path.join(tmp_path, 'users.json'))
        store.add_user('mike', 'pw1234', 'user')
        store.set_password('mike', 'newpassword')
        assert store.verify('mike', 'newpassword') is not None
        assert store.verify('mike', 'pw1234') is None
