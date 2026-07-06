"""Tests for the settings blueprint admin gating + behavior."""


class TestRoleGating:

    def test_settings_page_blocks_anonymous(self, client):
        resp = client.get('/settings')
        assert resp.status_code == 401

    def test_settings_page_blocks_user_role(self, auth_client):
        resp = auth_client.get('/settings')
        assert resp.status_code == 403

    def test_settings_page_allows_admin(self, admin_client):
        resp = admin_client.get('/settings')
        assert resp.status_code == 200
        assert b'DOCKD' in resp.data

    def test_api_settings_blocks_anonymous(self, client):
        resp = client.get('/api/settings')
        assert resp.status_code == 401

    def test_api_settings_blocks_user_role(self, auth_client):
        resp = auth_client.get('/api/settings')
        assert resp.status_code == 403

    def test_api_settings_allows_admin(self, admin_client):
        resp = admin_client.get('/api/settings')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'high_value_threshold' in body


class TestPublicSettings:

    def test_public_settings_requires_login(self, client):
        resp = client.get('/api/settings/public')
        assert resp.status_code == 401

    def test_public_settings_for_logged_in_user(self, auth_client):
        resp = auth_client.get('/api/settings/public')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'high_value_threshold' in body
        assert 'amazon_methods' in body
        # Internal-only fields must not leak.
        assert 'shiprush_accounts' not in body


class TestSettingsPatch:

    def test_patch_threshold(self, admin_client):
        resp = admin_client.patch(
            '/api/settings', json={'high_value_threshold': 350},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['settings']['high_value_threshold'] == 350

        # Round-trip via GET.
        resp2 = admin_client.get('/api/settings')
        assert resp2.get_json()['high_value_threshold'] == 350

    def test_patch_rejects_non_object(self, admin_client):
        resp = admin_client.patch('/api/settings', json=[1, 2, 3])
        assert resp.status_code == 400


# User CRUD and forced-password-change moved to Sentry (the identity
# provider); Dockd no longer exposes /api/users or /api/change-password.


class TestOverrideSkusPatch:
    """Settings page imports CSVs client-side and submits the parsed
    list via PATCH /api/settings. This test confirms the server-side
    half of that contract: a list of SKUs round-trips through the
    settings store."""

    def test_patch_replaces_override_list(self, admin_client):
        resp = admin_client.patch(
            '/api/settings',
            json={'override_exception_skus': ['ABC-1', 'XYZ-9']},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['settings']['override_exception_skus'] == ['ABC-1', 'XYZ-9']

    def test_patch_empty_list_clears(self, admin_client):
        admin_client.patch('/api/settings', json={'override_exception_skus': ['ABC-1']})
        admin_client.patch('/api/settings', json={'override_exception_skus': []})
        resp = admin_client.get('/api/settings')
        assert resp.get_json()['override_exception_skus'] == []


class TestSecretsAPI:

    def test_secrets_presence_admin_only(self, auth_client, admin_client):
        assert auth_client.get('/api/settings/secrets/presence').status_code == 403
        resp = admin_client.get('/api/settings/secrets/presence')
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'SHIPRUSH_TOKEN' in body

    def test_secrets_rejects_unknown_keys(self, admin_client):
        resp = admin_client.post('/api/settings/secrets', json={'NOT_A_KEY': 'x'})
        assert resp.status_code == 400


class TestDynamicRowEditPreservation:
    """v0.6.2: clicking "+ Add box" (or station, or fedex box) used to
    re-render the table from `settings.*` and wipe any in-progress
    edits the user had typed into existing rows. The fix calls the
    matching read*Table() before mutating the array. Same pattern
    applied to the per-row remove buttons.
    """

    def _settings_body(self, admin_client):
        resp = admin_client.get('/settings')
        assert resp.status_code == 200
        return resp.data.decode('utf-8')

    def _slice(self, body, fn_name):
        start = body.find('function ' + fn_name + '(')
        assert start >= 0, fn_name + ' not found in settings.html'
        end = body.find('function ', start + 10)
        return body[start:end]

    def test_add_box_row_reads_before_render(self, admin_client):
        block = self._slice(self._settings_body(admin_client), 'addBoxRow')
        assert 'readBoxesTable()' in block
        assert block.index('readBoxesTable()') < block.index('settings.boxes.push')

    def test_add_fedex_row_reads_before_render(self, admin_client):
        block = self._slice(self._settings_body(admin_client), 'addFedexRow')
        assert 'readFedexTable()' in block
        assert block.index('readFedexTable()') < block.index('settings.fedex_boxes.push')

    def test_add_station_row_reads_before_render(self, admin_client):
        block = self._slice(self._settings_body(admin_client), 'addStationRow')
        assert 'readStationsTable()' in block
        assert block.index('readStationsTable()') < block.index('settings.stations.push')

    def test_remove_handlers_read_before_splice(self, admin_client):
        body = self._settings_body(admin_client)
        # The inline remove handler for boxes/fedex/stations must read
        # the table before splicing. We assert the read call appears
        # immediately before each splice in the template.
        assert 'readBoxesTable(); settings.boxes.splice' in body
        assert 'readFedexTable(); settings.fedex_boxes.splice' in body
        assert 'readStationsTable(); settings.stations.splice' in body
