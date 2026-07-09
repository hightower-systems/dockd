"""Tests for the auth blueprint.

Login now verifies against Sentry (the identity provider) via
app.sentry_auth; tests drive it through the mock_sentry_auth fixture.
"""

from app.services.sentry_auth import (
    AccountLocked,
    InvalidCredentials,
    MustChangePassword,
    NotAuthorizedForDockd,
    ProviderUnavailable,
)


class TestLogin:

    def test_login_success(self, client, mock_sentry_auth):
        mock_sentry_auth.login.return_value = {'name': 'TestUser', 'role': 'user'}
        resp = client.post('/login', json={'username': 'TestUser', 'password': 'pw'})
        data = resp.get_json()
        assert data['status'] == 'success'
        assert data['user'] == {'name': 'TestUser', 'role': 'user'}

    def test_login_maps_admin_role(self, client, mock_sentry_auth):
        # SentryAuthenticator maps Sentry ADMIN -> dockd admin; the session
        # carries the mapped value.
        mock_sentry_auth.login.return_value = {'name': 'boss', 'role': 'admin'}
        resp = client.post('/login', json={'username': 'boss', 'password': 'pw'})
        assert resp.get_json()['user']['role'] == 'admin'

    def test_login_invalid_credentials(self, client, mock_sentry_auth):
        mock_sentry_auth.login.side_effect = InvalidCredentials()
        resp = client.post('/login', json={'username': 'x', 'password': 'bad'})
        assert resp.status_code == 401
        assert resp.get_json()['status'] == 'error'

    def test_login_must_change_password_blocked(self, client, mock_sentry_auth):
        mock_sentry_auth.login.side_effect = MustChangePassword()
        resp = client.post('/login', json={'username': 'seed', 'password': 'pw'})
        assert resp.status_code == 403
        assert 'Sentry' in resp.get_json()['message']

    def test_login_locked_passthrough(self, client, mock_sentry_auth):
        mock_sentry_auth.login.side_effect = AccountLocked('Locked for 15 minutes')
        resp = client.post('/login', json={'username': 'x', 'password': 'bad'})
        assert resp.status_code == 429
        assert 'Locked' in resp.get_json()['message']

    def test_login_provider_unavailable(self, client, mock_sentry_auth):
        mock_sentry_auth.login.side_effect = ProviderUnavailable('connect timeout')
        resp = client.post('/login', json={'username': 'x', 'password': 'pw'})
        assert resp.status_code == 503
        assert resp.get_json()['status'] == 'error'

    def test_login_not_authorized_for_dockd_blocked(self, client, mock_sentry_auth):
        # A USER without the `ship` grant is refused by the authenticator; the
        # blueprint surfaces it as a 403.
        mock_sentry_auth.login.side_effect = NotAuthorizedForDockd()
        resp = client.post('/login', json={'username': 'picker', 'password': 'pw'})
        assert resp.status_code == 403
        assert 'ship' in resp.get_json()['message'].lower()

    def test_login_sets_hard_capped_session_cookie(self, client, mock_sentry_auth):
        # A successful login is a permanent session with the 8h hard cap, so
        # the session cookie is bounded (carries an Expires ~8h out) rather
        # than being an unbounded browser-session cookie.
        from datetime import datetime, timedelta, timezone
        from email.utils import parsedate_to_datetime

        mock_sentry_auth.login.return_value = {'name': 'TestUser', 'role': 'user'}
        resp = client.post('/login', json={'username': 'TestUser', 'password': 'pw'})
        set_cookies = resp.headers.getlist('Set-Cookie')
        session_cookie = next(c for c in set_cookies if c.startswith('session='))
        assert 'Expires=' in session_cookie  # bounded, not a session cookie
        expires = parsedate_to_datetime(
            session_cookie.split('Expires=')[1].split(';')[0]
        )
        delta = expires - datetime.now(timezone.utc)
        assert timedelta(hours=7, minutes=59) <= delta <= timedelta(hours=8, minutes=1)


class TestSessionConfig:

    def test_session_is_hard_capped_not_sliding(self, app):
        from datetime import timedelta
        # Hard 8h cap: permanent + fixed lifetime + NOT refreshed each request,
        # so a kiosk session expires 8h after login regardless of activity.
        assert app.config['PERMANENT_SESSION_LIFETIME'] == timedelta(hours=8)
        assert app.config['SESSION_REFRESH_EACH_REQUEST'] is False

    def test_proxyfix_off_unless_trusted(self, app):
        from werkzeug.middleware.proxy_fix import ProxyFix
        # Footgun-safe default: proxy headers are NOT trusted unless the deploy
        # opts in with TRUST_PROXY (the test app does not set it).
        assert not isinstance(app.wsgi_app, ProxyFix)


class TestLogout:

    def test_logout(self, auth_client):
        resp = auth_client.post('/logout')
        assert resp.get_json()['status'] == 'success'

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

    def test_csrf_login_exempt(self, client, mock_sentry_auth):
        mock_sentry_auth.login.return_value = {'name': 'TestUser', 'role': 'user'}
        resp = client.post('/login', json={'username': 'TestUser', 'password': 'pw'},
                           headers={'Origin': 'http://evil.com'})
        assert resp.get_json()['status'] == 'success'


class TestHealth:

    def test_health(self, client):
        resp = client.get('/health')
        assert resp.status_code == 200
        assert resp.get_json()['status'] == 'ok'


class TestShutdown:

    def test_shutdown_admin_only(self, auth_client):
        resp = auth_client.post('/shutdown')
        assert resp.status_code == 403
