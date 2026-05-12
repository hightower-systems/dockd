"""Backend health monitor: cached on-demand probe of `backend.health()`.

Read-through cache with a lock so concurrent polls from multiple
pack stations do not turn into a stampede on the upstream
`/api/health` endpoint. The first request after the cache TTL
expires triggers a real network call; the others wait for the lock
and read the freshly-stored result.

Three states surfaced to the UI:

- `ok`        last probe succeeded within `cache_ttl_seconds`
- `degraded`  cache is stale (>= TTL) AND we have at least one
              successful probe in history; usually the in-flight
              probe will resolve to `ok` or `down` within seconds
- `down`      `failure_threshold` consecutive failures since the
              last success

Plus two extra-special cases:

- `not_configured`  the dockd container was started without a
                    backend (BACKEND env empty / unknown)
- `unknown`         no probe has run yet (cold start; the next
                    poll resolves this)
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger('dockd.backend_health')


class BackendHealth:

    def __init__(
        self,
        backend,
        *,
        cache_ttl_seconds: float = 30.0,
        failure_threshold: int = 3,
    ):
        self._backend = backend
        self._cache_ttl = float(cache_ttl_seconds)
        self._failure_threshold = int(failure_threshold)
        self._lock = threading.Lock()
        self._last_probe_at: Optional[float] = None
        self._last_success_at: Optional[float] = None
        self._consecutive_failures: int = 0
        self._last_error: Optional[str] = None
        self._last_state: str = 'unknown'

    def status(self) -> Dict[str, Any]:
        """Return the latest snapshot, probing if the cache is stale."""
        if self._backend is None:
            return {
                'state': 'not_configured',
                'message': 'No order backend wired. Set BACKEND=sentry in .env.',
                'consecutive_failures': 0,
                'last_success_at': None,
                'last_probe_at': None,
                'last_error': None,
            }

        now = time.monotonic()
        with self._lock:
            if self._last_probe_at is None or (now - self._last_probe_at) >= self._cache_ttl:
                self._probe_locked()
            return self._snapshot_locked()

    def _probe_locked(self) -> None:
        """Run a real `backend.health()`. Caller holds `self._lock`."""
        self._last_probe_at = time.monotonic()
        try:
            ok = bool(self._backend.health())
        except Exception as exc:
            ok = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("backend health probe raised: %s", exc)
        if ok:
            self._last_success_at = self._last_probe_at
            self._consecutive_failures = 0
            self._last_error = None
            self._last_state = 'ok'
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._last_state = 'down'
        else:
            # Stale-but-not-dead window. Still likely a blip.
            self._last_state = 'degraded'
        if self._last_error is None:
            self._last_error = 'health endpoint returned non-200'

    def _snapshot_locked(self) -> Dict[str, Any]:
        last_success_iso = None
        if self._last_success_at is not None:
            # last_success_at is a monotonic timestamp; convert to a
            # "seconds ago" delta which is more useful in the UI than
            # an absolute wall-clock value.
            last_success_iso = round(time.monotonic() - self._last_success_at, 1)
        last_probe_iso = None
        if self._last_probe_at is not None:
            last_probe_iso = round(time.monotonic() - self._last_probe_at, 1)
        return {
            'state': self._last_state,
            'consecutive_failures': self._consecutive_failures,
            'last_success_seconds_ago': last_success_iso,
            'last_probe_seconds_ago': last_probe_iso,
            'last_error': self._last_error,
            'cache_ttl_seconds': self._cache_ttl,
            'failure_threshold': self._failure_threshold,
        }
