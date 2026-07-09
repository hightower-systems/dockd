"""Unit tests for SentryAuthenticator (Sentry-as-IdP login)."""

import httpx
import pytest

from app.services.sentry_auth import (
    AccountLocked,
    InvalidCredentials,
    MustChangePassword,
    NotAuthorizedForDockd,
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
        # A USER must carry the `ship` grant to pass the pack-station gate.
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'joe', 'role': 'USER', 'must_change_password': False,
            'allowed_functions': ['pick', 'pack', 'ship']}}))
        assert auth.login('joe', 'pw')['role'] == 'user'

    def test_unknown_role_defaults_to_user(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'x', 'role': 'WEIRD', 'allowed_functions': ['ship']}}))
        assert auth.login('x', 'pw')['role'] == 'user'

    def test_hits_the_login_path(self):
        auth, client = _auth(_Resp(200, {'user': {
            'username': 'x', 'role': 'USER', 'allowed_functions': ['ship']}}))
        auth.login('x', 'pw')
        assert client.calls[0]['url'] == 'https://sentry.test/api/auth/login'
        assert client.calls[0]['json'] == {'username': 'x', 'password': 'pw'}

    def test_forwards_client_ip(self):
        auth, client = _auth(_Resp(200, {'user': {
            'username': 'x', 'role': 'USER', 'allowed_functions': ['ship']}}))
        auth.login('x', 'pw', client_ip='1.2.3.4')
        assert client.calls[0]['headers'].get('X-Forwarded-For') == '1.2.3.4'


class TestShipGate:
    """Non-ADMIN accounts must hold the `ship` allowed_function; ADMIN is
    exempt (holds every function implicitly)."""

    def test_user_without_ship_is_refused(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'picker', 'role': 'USER',
            'allowed_functions': ['pick', 'count']}}))
        with pytest.raises(NotAuthorizedForDockd):
            auth.login('picker', 'pw')

    def test_user_with_empty_functions_is_refused(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'blank', 'role': 'USER'}}))
        with pytest.raises(NotAuthorizedForDockd):
            auth.login('blank', 'pw')

    def test_user_with_ship_is_allowed(self):
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'packer', 'role': 'USER',
            'allowed_functions': ['pick', 'pack', 'ship']}}))
        assert auth.login('packer', 'pw') == {'name': 'packer', 'role': 'user'}

    def test_admin_without_functions_is_exempt(self):
        # ADMIN with an empty allowed_functions still ships (matches the two
        # real ADMIN accounts that carry no explicit functions).
        auth, _ = _auth(_Resp(200, {'user': {
            'username': 'boss', 'role': 'ADMIN', 'allowed_functions': []}}))
        assert auth.login('boss', 'pw') == {'name': 'boss', 'role': 'admin'}


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
