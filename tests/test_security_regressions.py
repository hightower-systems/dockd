"""Security regression suite (v0.5.0).

Cross-cutting assertions that don't fit one module. Every test here
guards against a class of leak / bypass that has burned us before
or that the threat model explicitly forbids.

Categories:
  1. Bearer tokens never appear in log output (RedactionFilter +
     log-statement hygiene).
  2. TLS validation cannot be turned off through configuration.
  3. Sensitive fields never appear in API responses (e.g. /api/users
     returns role + username but never the password hash).
  4. settings.json + users.json have chmod 600 perms after every
     write, including patches and password updates.
"""

import inspect
import logging
import os
import re
import stat

import httpx

from app.logging_config import RedactionFilter
from app.services.backend.sentry import SentryBackend


# ----------------------------------------------------------------------
# 1. Token + bearer leak guards
# ----------------------------------------------------------------------


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


class TestNoTokenLeak:

    def test_wms_token_not_in_logs_after_redaction(self):
        log = logging.getLogger('dockd.test.security.token')
        log.handlers = []
        log.propagate = False
        log.setLevel(logging.DEBUG)
        h = _Capture()
        h.setFormatter(logging.Formatter('%(message)s'))
        h.addFilter(RedactionFilter())
        log.addHandler(h)

        log.info(
            "Outbound: X-WMS-Token: wms_t_thisIsTheSecretValue_AAAA. Tracking 1Z999.",
        )
        log.info("Authorization: Bearer bearer.token.here-is-the-secret")
        # Even unstructured prose with the token inline gets caught:
        log.warning("Got 401 with wms_t_anotherSecretValueHere_BBBB")

        all_lines = '\n'.join(h.lines)
        # The redacted prefix is allowed; the suffix must be gone.
        assert 'thisIsTheSecretValue' not in all_lines
        assert 'bearer.token.here-is-the-secret' not in all_lines
        assert 'anotherSecretValueHere' not in all_lines


# ----------------------------------------------------------------------
# 2. TLS validation cannot be disabled
# ----------------------------------------------------------------------


class TestTLSEnforced:

    def test_sentrybackend_has_no_verify_kwarg(self):
        """Construction signature does not accept a `verify` knob.

        Adding one would be a regression against the threat model.
        """
        sig = inspect.signature(SentryBackend.__init__)
        assert 'verify' not in sig.parameters, (
            "SentryBackend must not expose a `verify` kwarg; TLS "
            "validation is non-negotiable."
        )

    def test_sentrybackend_constructs_verify_true(self):
        """When the default httpx.Client path is taken, verify=True
        is hardcoded. Confirm via source inspection so this fails
        loudly if someone parameterizes it later."""
        source = inspect.getsource(SentryBackend.__init__)
        assert 'verify=True' in source
        # And no path sets it to False, off, 0, or pulls from env.
        assert not re.search(r'verify\s*=\s*(False|0|os\.environ)', source)


# ----------------------------------------------------------------------
# 3. API responses must not expose hashes
# ----------------------------------------------------------------------


class TestNoHashInResponses:

    def test_list_users_omits_password_hash(self, admin_client):
        resp = admin_client.get('/api/users')
        assert resp.status_code == 200
        body = resp.get_json()
        for user in body:
            # The store records `password_hash`; the API must not
            # echo it.
            assert 'password_hash' not in user
            for k, v in user.items():
                if isinstance(v, str):
                    assert not v.startswith('scrypt:'), \
                        f"Field {k} appears to be a scrypt hash"

    def test_settings_secrets_presence_never_returns_values(self, admin_client):
        resp = admin_client.get('/api/settings/secrets/presence')
        assert resp.status_code == 200
        body = resp.get_json()
        for key, val in body.items():
            # Presence endpoint returns booleans only.
            assert isinstance(val, bool), f"{key} returned a non-bool: {val!r}"


# ----------------------------------------------------------------------
# 4. File-perm guards (chmod 600 on every write)
# ----------------------------------------------------------------------


class TestFilePermissions:

    def test_settings_json_is_600_after_patch(self, admin_client, app):
        path = app.settings_store.path
        admin_client.patch('/api/settings', json={'high_value_threshold': 250})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_users_json_is_600_after_password_set(self, admin_client, app):
        path = app.users_store.path
        app.users_store.add_user('regression_user', 'pw1234', 'user')
        try:
            admin_client.put(
                '/api/users/regression_user/password',
                json={'password': 'newpw5678'},
            )
            mode = stat.S_IMODE(os.stat(path).st_mode)
            assert mode == 0o600
        finally:
            app.users_store.remove_user('regression_user')


# ----------------------------------------------------------------------
# 5. Backend timeouts are bounded
# ----------------------------------------------------------------------


class TestTimeoutsBounded:

    def test_sentrybackend_default_timeout_is_finite(self):
        sig = inspect.signature(SentryBackend.__init__)
        timeout_default = sig.parameters['timeout'].default
        assert isinstance(timeout_default, (int, float))
        assert 0 < timeout_default <= 60, \
            f"SentryBackend default timeout is {timeout_default}s; should be a reasonable bound"
