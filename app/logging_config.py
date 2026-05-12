"""Structured JSON logging for Dockd.

Two filters wrap every record:

- `RedactionFilter` -- scrubs Sentry-style bearer tokens (`wms_t_*`)
  and `Authorization: Bearer ...` strings from the formatted
  message text. Defense in depth: log-statement authors should not
  pass secrets in the first place, but a regression on that gate
  must not also leak the secret to disk.
- `JSONFormatter` -- structured one-line JSON per record, with
  Flask-request extras (`method`, `path`, `status_code`, etc.)
  promoted to top-level fields when present.
"""

import json
import logging
import os
import re
from logging.handlers import RotatingFileHandler


# Sentry bearer tokens have the `wms_t_` prefix and base64-ish suffix.
# Match aggressively (any length 6+) so partial values are caught too.
_WMS_TOKEN_RE = re.compile(r'wms_t_[A-Za-z0-9_\-]{6,}')

# Generic `Authorization: Bearer ...` or `Authorization=Bearer ...`.
_BEARER_RE = re.compile(
    r'(?i)(authorization\s*[:=]\s*)(bearer\s+)([A-Za-z0-9_\-.=]+)'
)

# `X-Sentry-Token: ...` / `X-WMS-Token: ...` (case-insensitive).
_TOKEN_HEADER_RE = re.compile(
    r'(?i)(x-(?:sentry|wms)-token\s*[:=]\s*)([A-Za-z0-9_\-.=]+)'
)


_REDACTED = '<REDACTED>'


def _scrub(text: str) -> str:
    if not text:
        return text
    if 'wms_t_' in text:
        text = _WMS_TOKEN_RE.sub('wms_t_<REDACTED>', text)
    if 'earer' in text.lower():
        text = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}", text)
    if '-token' in text.lower():
        text = _TOKEN_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
    return text


class RedactionFilter(logging.Filter):
    """Scrub bearer tokens from every log record.

    Mutates `record.msg` (and clears `record.args` when we touched
    the message) so the formatter sees the redacted text. Operates
    on the rendered message, not on the raw `msg` format string, so
    interpolated argument values are scrubbed too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        scrubbed = _scrub(rendered)
        if scrubbed is not rendered and scrubbed != rendered:
            record.msg = scrubbed
            record.args = None
        # Also scrub any extras the JSON formatter promotes.
        for attr in ('method', 'path', 'user'):
            val = getattr(record, attr, None)
            if isinstance(val, str):
                new_val = _scrub(val)
                if new_val != val:
                    setattr(record, attr, new_val)
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if hasattr(record, 'method'):
            log_entry['method'] = record.method
        if hasattr(record, 'path'):
            log_entry['path'] = record.path
        if hasattr(record, 'status_code'):
            log_entry['status_code'] = record.status_code
        if hasattr(record, 'response_time_ms'):
            log_entry['response_time_ms'] = record.response_time_ms
        if hasattr(record, 'user'):
            log_entry['user'] = record.user
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_logging(log_dir='logs', level='INFO'):
    logger = logging.getLogger('dockd')
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    redaction = RedactionFilter()

    console = logging.StreamHandler()
    console.setFormatter(JSONFormatter())
    console.addFilter(redaction)
    logger.addHandler(console)

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, 'dockd.log'),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setFormatter(JSONFormatter())
        file_handler.addFilter(redaction)
        logger.addHandler(file_handler)

    return logger
