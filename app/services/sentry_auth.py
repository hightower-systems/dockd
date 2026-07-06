"""Sentry-WMS as Dockd's identity provider.

Dockd no longer stores users or passwords. A human login is verified by
POSTing the credentials to Sentry's ``POST /api/auth/login``; on success we
read the user's role from the JSON body and establish a Dockd Flask
session. The 8h JWT Sentry returns is intentionally discarded -- Dockd's
ship/void backend calls use the separate ``X-WMS-Token`` service token, and
Dockd's own session is its signed cookie.

Sentry enforces the (IP, username) lockout inside its login endpoint, so we
forward the operator's real IP as ``X-Forwarded-For`` and surface Sentry's
429 verbatim.
"""

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger('dockd.sentry_auth')


class AuthError(Exception):
    """Base for login outcomes that are not a clean success."""


class InvalidCredentials(AuthError):
    """Sentry returned 401 -- wrong username/password, or inactive account
    (Sentry authenticates only active users, and never reveals which)."""


class AccountLocked(AuthError):
    """Sentry returned 429 -- its (IP, username) lockout tripped."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class MustChangePassword(AuthError):
    """Credentials are valid but Sentry flags the account for forced change.
    Dockd has no password UI, so the operator rotates in Sentry first."""


class ProviderUnavailable(AuthError):
    """Sentry could not be reached / returned an unexpected status."""


# Sentry roles are uppercase; Dockd sessions/frontend use lowercase.
_ROLE_MAP = {'ADMIN': 'admin', 'USER': 'user'}


class SentryAuthenticator:
    """Verifies human logins against Sentry's auth API."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        client: Optional[httpx.Client] = None,
    ):
        self._base_url = (base_url or '').rstrip('/')
        # verify=True hardcoded; no env knob disables TLS validation (matches
        # SentryBackend). Test fixtures inject a stub client.
        self._client = client or httpx.Client(timeout=timeout, verify=True)

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def login(
        self,
        username: str,
        password: str,
        client_ip: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify credentials against Sentry.

        Returns ``{'name': str, 'role': 'admin'|'user'}`` on success, or
        raises a typed AuthError subclass. The Sentry JWT is not returned --
        Dockd holds only a session.
        """
        if not self._base_url:
            raise ProviderUnavailable("SENTRY_BASE_URL is not configured")

        headers = {}
        if client_ip:
            # Let Sentry's (IP, username) lockout key on the real operator IP
            # rather than Dockd's server IP.
            headers['X-Forwarded-For'] = client_ip

        try:
            resp = self._client.post(
                f"{self._base_url}/api/auth/login",
                json={'username': username, 'password': password},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("Sentry auth unreachable: %s", exc)
            raise ProviderUnavailable(str(exc))

        if resp.status_code == 200:
            user = (resp.json() or {}).get('user') or {}
            if user.get('must_change_password'):
                raise MustChangePassword()
            role = _ROLE_MAP.get((user.get('role') or '').upper(), 'user')
            return {'name': user.get('username') or username, 'role': role}

        if resp.status_code == 401:
            raise InvalidCredentials()

        if resp.status_code == 429:
            message = ''
            try:
                message = (resp.json() or {}).get('error', '')
            except Exception:
                pass
            raise AccountLocked(message or 'Too many failed login attempts.')

        logger.warning("Sentry auth returned unexpected status %s", resp.status_code)
        raise ProviderUnavailable(f"unexpected status {resp.status_code}")


__all__ = [
    'SentryAuthenticator',
    'AuthError',
    'InvalidCredentials',
    'AccountLocked',
    'MustChangePassword',
    'ProviderUnavailable',
]
