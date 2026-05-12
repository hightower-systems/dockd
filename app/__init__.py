"""Dockd application factory."""

import logging
import os
import sys
from flask import Flask, g, has_request_context

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


def _build_backend(config):
    """Construct the order backend chosen by env (BACKEND=sentry).

    Returns None when no backend is configured; ShippingService surfaces
    a structured "backend not configured" error on every order-touching
    route in that state.
    """
    log = logging.getLogger('dockd')
    backend_name = (os.environ.get('BACKEND') or '').strip().lower()
    if not backend_name:
        log.info("No BACKEND env set; order-routing endpoints disabled.")
        return None
    if backend_name == 'sentry':
        base_url = (os.environ.get('SENTRY_BASE_URL') or '').strip()
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
    app.secret_key = config.SECRET_KEY
    app.config['SESSION_PERMANENT'] = False
    app.config['VERSION'] = config.VERSION

    # Extensions
    limiter.init_app(app)

    # Settings + user stores. JSON files at project root by default;
    # override locations with SETTINGS_PATH / USERS_PATH env vars (used
    # by tests and Azure volume mounts).
    from app.services.settings import SettingsStore
    from app.services.users_store import UsersStore

    settings_path = os.environ.get(
        'SETTINGS_PATH',
        os.path.join(os.getcwd(), 'settings.json'),
    )
    users_path = os.environ.get(
        'USERS_PATH',
        os.path.join(os.getcwd(), 'users.json'),
    )
    app.settings_store = SettingsStore(settings_path)
    app.users_store = UsersStore(users_path)

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
        history_file='ship_history.json',
        label_dir='label_history',
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
    backend = _build_backend(config)
    app.shipping_service = ShippingService(
        backend=backend,
        shiprush=shiprush,
        carrier_engine=carrier_engine,
        printer=printer,
        label_cache=label_cache,
        config=config,
    )
    app.scale_reader = scale

    # Register blueprints
    from app.blueprints.auth import auth_bp
    from app.blueprints.shipping import shipping_bp
    from app.blueprints.settings import settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(shipping_bp)
    app.register_blueprint(settings_bp)

    # Initialize databases
    from app.models.database import init_all_dbs
    init_all_dbs()

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

    logger.info("Dockd v%s initialized", config.VERSION)

    return app
