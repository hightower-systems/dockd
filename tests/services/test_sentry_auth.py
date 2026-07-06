"""Unit tests for SentryAuthenticator (Sentry-as-IdP login)."""

import httpx
import pytest

from app.services.sentry_auth import (
    AccountLocked,
    InvalidCredentials,
    MustChangePassword,
    ProviderUnavailable,
    SentryAuthenticator,
)


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({'url': url, 'json': json, 'headers': headers or {}})
        if self._exc:
            raise self._exc
        return self._resp


def _auth(resp=None, exc=None):
    client = _FakeClient(resp, exc)
    return SentryAuthenticator(base_url='https://sentry.test', client=client), client


class TestSuccess:

    def test_maps_admin_role(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'boss', 'role': 'ADMIN', 'must_change_password': False}}))
        assert auth.login('boss', 'pw') == {'name': 'boss', 'role': 'admin'}

    def test_maps_user_role(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'joe', 'role': 'USER', 'must_change_password': False}}))
        assert auth.login('joe', 'pw')['role'] == 'user'

    def test_unknown_role_defaults_to_user(self):
        auth, _ = _auth(_Resp(200, {'user': {'username': 'x', 'role': 'WEIRD'}}))
        assert auth.login('x', 'pw')['role'] == 'user'

    def test_hits_the_login_path(self):
        auth, client = _auth(_Resp(200, {'user': {'username': 'x', 'role': 'USER'}}))
        auth.login('x', 'pw')
        assert client.calls[0]['url'] == 'https://sentry.test/api/auth/login'
        assert client.calls[0]['json'] == {'username': 'x', 'password': 'pw'}

    def test_forwards_client_ip(self):
        auth, client = _auth(_Resp(200, {'user': {'username': 'x', 'role': 'USER'}}))
        auth.login('x', 'pw', client_ip='1.2.3.4')
        assert client.calls[0]['headers'].get('X-Forwarded-For') == '1.2.3.4'


class TestFailures:

    def test_must_change_password_raises(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'seed', 'role': 'ADMIN', 'must_change_password': True}}))
        with pytest.raises(MustChangePassword):
            auth.login('seed', 'pw')

    def test_401_invalid_credentials(self):
        auth, _ = _auth(_Resp(401, {'error': 'Invalid username or password'}))
        with pytest.raises(InvalidCredentials):
            auth.login('x', 'bad')

    def test_429_locked_preserves_message(self):
        auth, _ = _auth(_Resp(429, {'error': 'Locked for 15 minutes'}))
        with pytest.raises(AccountLocked) as ei:
            auth.login('x', 'bad')
        assert ei.value.message == 'Locked for 15 minutes'

    def test_5xx_is_unavailable(self):
        auth, _ = _auth(_Resp(500, {}))
        with pytest.raises(ProviderUnavailable):
            auth.login('x', 'pw')

    def test_network_error_is_unavailable(self):
        auth, _ = _auth(exc=httpx.ConnectError('boom'))
        with pytest.raises(ProviderUnavailable):
            auth.login('x', 'pw')

    def test_missing_base_url_is_unavailable(self):
        auth = SentryAuthenticator(base_url='')
        assert auth.configured is False
        with pytest.raises(ProviderUnavailable):
            auth.login('x', 'pw')
