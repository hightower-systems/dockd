"""Dockd application factory."""

import logging
import os
import sys
from datetime import timedelta

from flask import Flask, g, has_request_context
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.extensions import limiter
from app.logging_config import setup_logging


def resource_path(relative_path):
    """Resolve path for PyInstaller bundles or normal execution."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(os.path.dirname(__file__))
    return os.path.join(base_path, relative_path)


def _resolve_sentry_token():
    """Return the per-request Sentry token.

    Preference order:
      1. Per-request value stashed on `flask.g.sentry_token` (set by
         the shipping blueprint's before-request hook when the browser
         sends `X-Sentry-Token`). This is the path scale-agent v2 will
         drive once it lands.
      2. `DOCKD_SENTRY_TOKEN` env var -- single-token interim source
         for v0.2.0 so the integration is usable before scale-agent v2.

    Returns an empty string if neither is set; the Sentry API will
    return 401 invalid_token and the user-facing error explains the
    misconfiguration.
    """
    if has_request_context():
        token = getattr(g, 'sentry_token', None)
        if token:
            return token
    return (os.environ.get('DOCKD_SENTRY_TOKEN') or '').strip()


def _build_backend(config, sentry_base_url):
    """Construct the order backend chosen by env (BACKEND=sentry).

    Returns None when no backend is configured; ShippingService surfaces
    a structured "backend not configured" error on every order-touching
    route in that state. ``sentry_base_url`` is resolved once by the caller
    (the same value the login authenticator uses) so the app factory reads
    SENTRY_BASE_URL from the environment in exactly one place.
    """
    log = logging.getLogger('dockd')
    backend_name = (os.environ.get('BACKEND') or '').strip().lower()
    if not backend_name:
        log.info("No BACKEND env set; order-routing endpoints disabled.")
        return None
    if backend_name == 'sentry':
        base_url = sentry_base_url
        if not base_url:
            log.warning(
                "BACKEND=sentry but SENTRY_BASE_URL is empty; falling back "
                "to backend=None.",
            )
            return None
        from app.services.backend.sentry import SentryBackend
        log.info("Order backend: SentryBackend(base_url=%s)", base_url)
        return SentryBackend(base_url=base_url, get_token=_resolve_sentry_token)
    log.warning("BACKEND=%s is not a known value; backend=None.", backend_name)
    return None


def create_app(config_class=None):
    config = config_class or Config

    logger = setup_logging(log_dir=config.LOG_DIR, level=config.LOG_LEVEL)

    app = Flask(__name__, template_folder=resource_path('templates'))
    # Behind Azure Container Apps ingress (one proxy hop), trust X-Forwarded-*
    # so request.remote_addr is the operator's real IP, not the shared ingress
    # address. That makes the X-Forwarded-For Dockd forwards to Sentry's
    # (IP, username) lockout accurate and stops the /login rate limiter from
    # bucketing every station onto one address.
    #
    # Gated behind TRUST_PROXY (default off), mirroring Sentry's opt-in
    # posture: honoring these headers when NOT behind a trusted proxy lets any
    # client on the LAN forge its own IP -- the well-known ProxyFix footgun.
    # The operator sets TRUST_PROXY=true only where the ingress controls the
    # network (the ACA deploy).
    trust_proxy = os.environ.get('TRUST_PROXY', '').lower() in ('true', '1', 'yes')
    if trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    logger.warning(
        "ProxyFix %s",
        "active: trusting X-Forwarded-* (TRUST_PROXY set)" if trust_proxy
        else "inactive: not trusting proxy headers (TRUST_PROXY unset)",
    )
    app.secret_key = config.SECRET_KEY
    # A logged-in session is a hard-capped cookie: it expires
    # SESSION_LIFETIME_HOURS after login and is NOT refreshed per request, so a
    # kiosk browser that never closes still forces re-auth (re-checking
    # Sentry's is_active) within a shift. login() sets session.permanent so the
    # lifetime applies to the session it creates.
    app.config['SESSION_PERMANENT'] = True
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(
        hours=config.SESSION_LIFETIME_HOURS
    )
    app.config['SESSION_REFRESH_EACH_REQUEST'] = False
    app.config['VERSION'] = config.VERSION

    # Extensions
    limiter.init_app(app)

    # Postgres connection pool. Schema is owned by Alembic (alembic upgrade
    # head runs at container boot); the Postgres-backed stores below need
    # the pool ready, so initialize it first.
    from app.models.database import init_pool
    init_pool(maxconn=config.DB_POOL_MAX)

    # Settings are Postgres-backed (Phase 2c). Identity is Sentry's: /login
    # verifies against Sentry's auth API, so Dockd has no user store. Only
    # the .env secrets surface and the local label cache stay on disk.
    from app.services.settings import SettingsStore
    from app.services.sentry_auth import SentryAuthenticator

    app.settings_store = SettingsStore()
    # Reuse the same Sentry host the order backend targets. Empty until
    # configured; login then returns 503 (identity provider unreachable).
    sentry_base_url = (os.environ.get('SENTRY_BASE_URL') or '').strip()
    app.sentry_auth = SentryAuthenticator(base_url=sentry_base_url)

    # Filesystem-backed local artifacts (label cache + raw label history).
    # 8h-ephemeral by design; DATA_DIR points them at a mount in prod.
    data_dir = os.environ.get('DATA_DIR') or os.getcwd()

    # Build services (bottom-up, no import-time side effects)
    from app.services.label_cache import LabelCache
    from app.services.shiprush import ShipRushClient
    from app.services.carrier import CarrierEngine
    from app.services.printer import PrinterService
    from app.services.scale import ScaleReader
    from app.services.shipping import ShippingService

    label_max_age_hours = int(
        app.settings_store.get('label_max_age_hours', config.LABEL_MAX_AGE_HOURS)
    )
    label_cache = LabelCache(
        history_file=os.path.join(data_dir, 'ship_history.json'),
        label_dir=os.path.join(data_dir, 'label_history'),
        max_age_hours=label_max_age_hours,
    )
    shiprush = ShipRushClient(app.settings_store, label_cache)

    rural_zip_path = resource_path(os.path.join('data', 'ups_area_surcharge_zips.csv'))
    carrier_engine = CarrierEngine(app.settings_store, rural_zip_path=rural_zip_path)

    printer = PrinterService(app.settings_store, config=config)
    scale = ScaleReader(config.SCALE_VENDOR_ID, config.SCALE_PRODUCT_ID)

    # Order backend selection. v0.2.0 supports `BACKEND=sentry`; an
    # unset / empty / unknown value leaves backend=None and the
    # load_order / ship_order / manual_link / void(write-back) routes
    # return a structured "backend not configured" error.
    backend = _build_backend(config, sentry_base_url)
    app.shipping_service = ShippingService(
        backend=backend,
        shiprush=shiprush,
        carrier_engine=carrier_engine,
        printer=printer,
        label_cache=label_cache,
        config=config,
        settings=app.settings_store,
    )
    app.scale_reader = scale

    # Backend health monitor (v0.5.0). Cached on-demand probe: the
    # first /api/health/backend poll after the cache TTL expires
    # triggers a real backend.health() call; concurrent polls share
    # the result. Read by the operator-UI connectivity dot.
    from app.services.backend_health import BackendHealth
    app.backend_health = BackendHealth(backend)

    # Thread catalog for the item-barcode labels tab. The CSV ships in
    # the app/data/ directory; resource_path resolves it correctly for
    # both PyInstaller bundles and normal execution.
    from app.services.labels import ThreadCatalog
    thread_catalog_path = resource_path(os.path.join('data', 'thread_catalog.csv'))
    app.thread_catalog = ThreadCatalog(thread_catalog_path)

    # Register blueprints
    from app.blueprints.auth import auth_bp
    from app.blueprints.shipping import shipping_bp
    from app.blueprints.settings import settings_bp
    from app.blueprints.labels import labels_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(shipping_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(labels_bp)

    # Crash-recovery: retry any ship_attempts rows that the previous
    # process left mid-flight. Opt-in via env so test boots and CI do
    # not hammer the backend. In production set
    # DOCKD_RETRY_PENDING_ON_BOOT=true.
    if (os.environ.get('DOCKD_RETRY_PENDING_ON_BOOT') or '').lower() in ('1', 'true', 'yes'):
        if backend is None:
            logger.info(
                "DOCKD_RETRY_PENDING_ON_BOOT set but no backend wired; skipping retry.",
            )
        else:
            try:
                results = app.shipping_service.retry_recoverable_attempts()
                if results:
                    logger.info(
                        "Boot-time retry drained %d ship_attempts row(s): %s",
                        len(results),
                        ', '.join(f"{k[:8]}->{s}" for k, s in results),
                    )
            except Exception as exc:
                logger.error("Boot-time retry of ship_attempts failed: %s", exc)

    # Periodic in-process retry of unknown ship_attempts (v0.5.0).
    # Complements boot-time retry: catches the case where a transient
    # network blip recovers a few minutes after the ship and the
    # process never restarts. Opt-in via the env knob (in seconds).
    # Defaults to off; production deployments set
    # DOCKD_RETRY_POLL_INTERVAL=300 (5 minutes) or similar.
    retry_interval_s = int((os.environ.get('DOCKD_RETRY_POLL_INTERVAL') or '0').strip() or 0)
    if retry_interval_s > 0 and backend is not None:
        _start_periodic_retry(app, interval_seconds=retry_interval_s)

    logger.info("Dockd v%s initialized", config.VERSION)

    return app


def _start_periodic_retry(app, *, interval_seconds: int):
    """Daemon thread that drains pending/unknown ship_attempts rows.

    Catches a transient network blip that clears up after the
    original ship but before the next dockd restart. Idempotent by
    construction: Sentry's dockd_idempotency replays the cached
    response if the original committed.
    """
    import threading
    log = logging.getLogger('dockd')

    def _loop():
        while True:
            try:
                # Each tick gets the latest ShippingService in case
                # of a config reload that swapped backends.
                results = app.shipping_service.retry_recoverable_attempts()
                if results:
                    log.info(
                        "Periodic retry drained %d ship_attempts row(s)",
                        len(results),
                    )
            except Exception as exc:
                log.error("Periodic retry tick failed: %s", exc)
            import time as _t
            _t.sleep(interval_seconds)

    t = threading.Thread(
        target=_loop, name='dockd-periodic-retry', daemon=True,
    )
    t.start()
    logging.getLogger('dockd').info(
        "Periodic ship_attempts retry enabled (every %ds)", interval_seconds,
    )
