"""Tests for auth blueprint - login, logout, CSRF, login_required."""

import pytest


class TestLogin:

    def test_login_success(self, client):
        resp = client.post('/login', json={'username': 'TestUser', 'password': 'testpass123'})
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['user']['name'] == 'TestUser'

    def test_login_wrong_password(self, client):
        resp = client.post('/login', json={'username': 'TestUser', 'password': 'wrong'})
        data = resp.get_json()
        assert data['status'] == 'error'

    def test_login_unknown_user(self, client):
        resp = client.post('/login', json={'username': 'NoUser', 'password': 'test'})
        data = resp.get_json()
        assert data['status'] == 'error'

    def test_login_empty(self, client):
        resp = client.post('/login', json={'username': '', 'password': ''})
        data = resp.get_json()
        assert data['status'] == 'error'


class TestLogout:

    def test_logout(self, auth_client):
        resp = auth_client.post('/logout')
        data = resp.get_json()
        assert data['status'] == 'success'

    def test_logout_unauthenticated(self, client):
        resp = client.post('/logout', json={})
        assert resp.status_code == 401


class TestLoginRequired:

    def test_protected_route_unauthenticated(self, client):
        resp = client.get('/get_scale_weight')
        # GET returns JS redirect for browser, not 401
        assert resp.status_code == 200
        assert b'window.location' in resp.data

    def test_protected_route_authenticated(self, auth_client):
        resp = auth_client.get('/get_scale_weight')
        assert resp.status_code == 200


class TestCSRF:

    def test_csrf_blocks_foreign_origin(self, auth_client):
        resp = auth_client.post('/logout', json={},
                                headers={'Origin': 'http://evil.com'})
        assert resp.status_code == 403

    def test_csrf_allows_localhost(self, auth_client):
        resp = auth_client.post('/logout', json={},
                                headers={'Origin': 'http://127.0.0.1:5001'})
        assert resp.status_code == 200

    def test_csrf_allows_local_network(self, auth_client):
        resp = auth_client.post('/logout', json={},
                                headers={'Origin': 'http://10.0.0.5:5001'})
        assert resp.status_code == 200

    def test_csrf_login_exempt(self, client):
        resp = client.post('/login', json={'username': 'TestUser', 'password': 'testpass123'},
                           headers={'Origin': 'http://evil.com'})
        data = resp.get_json()
        assert data['status'] == 'success'


class TestHealth:

    def test_health(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'


class TestShutdown:

    def test_shutdown_admin_only(self, auth_client):
        resp = auth_client.post('/shutdown')
        assert resp.status_code == 403
