"""Dockd application factory."""

import os
import sys
from flask import Flask

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

    # Wire orchestrators onto app. `backend` is None until the Sentry
    # backend lands; order-loading and tracking-writeback paths return
    # a structured "backend not configured" error until then. ShipRush,
    # label cache, printer, and override audit paths work today.
    app.shipping_service = ShippingService(
        backend=None,
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

    logger.info("Dockd v%s initialized", config.VERSION)

    return app
