"""Unit tests for the log redaction filter."""

import logging
import re

import pytest

from app.logging_config import RedactionFilter, _scrub


class _CapturingHandler(logging.Handler):
    """Stash every formatted record's message for assertion."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


@pytest.fixture
def logger_with_redaction():
    log = logging.getLogger('dockd.test.redaction.' + str(id(object())))
    log.setLevel(logging.DEBUG)
    log.propagate = False
    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    handler.addFilter(RedactionFilter())
    log.addHandler(handler)
    yield log, handler
    log.removeHandler(handler)


class TestScrub:

    def test_strips_full_wms_token(self):
        out = _scrub("X-WMS-Token: wms_t_AbCdEf123456_XYZ-deadbeef")
        assert 'wms_t_AbCdEf' not in out
        assert '<REDACTED>' in out

    def test_strips_bare_wms_token_in_text(self):
        out = _scrub("Sentry returned: wms_t_supersecretvalue is bad")
        assert 'supersecret' not in out
        # The wms_t_ prefix is preserved + suffix replaced; or
        # combined with the header replacement -- either way the
        # secret bytes are gone.
        assert 'supersecret' not in out

    def test_strips_bearer(self):
        out = _scrub("Authorization: Bearer abc.def.ghi-token")
        assert 'abc.def.ghi-token' not in out
        assert 'Bearer <REDACTED>' in out

    def test_strips_xsentry_header(self):
        out = _scrub("X-Sentry-Token=wms_t_1234567890abcdef")
        assert 'wms_t_1234567890' not in out

    def test_idempotent(self):
        once = _scrub("X-WMS-Token: wms_t_foooooooooo")
        twice = _scrub(once)
        assert once == twice

    def test_pass_through_when_no_secret(self):
        out = _scrub("normal log line with order SO-1001 tracking 1Z999")
        assert out == "normal log line with order SO-1001 tracking 1Z999"


class TestRedactionFilter:

    def test_filter_scrubs_format_args(self, logger_with_redaction):
        log, handler = logger_with_redaction
        log.info("Token=%s", "wms_t_abc123def456ghi789")
        out = handler.records[-1]
        assert 'abc123def456' not in out
        assert '<REDACTED>' in out

    def test_filter_scrubs_inline_string(self, logger_with_redaction):
        log, handler = logger_with_redaction
        log.info("Authorization: Bearer toptoptopsecret_value")
        out = handler.records[-1]
        assert 'toptoptopsecret' not in out

    def test_filter_passes_through_clean(self, logger_with_redaction):
        log, handler = logger_with_redaction
        log.info("Shipped SO-1001 via UPS")
        assert handler.records[-1] == "Shipped SO-1001 via UPS"
