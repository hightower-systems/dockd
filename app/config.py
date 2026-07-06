"""
Centralized configuration for Dockd.

Values are read from environment variables with sensible defaults.
Override via .env file or system environment.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    VERSION = '1.1.0'

    # -- Flask --
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        import warnings
        warnings.warn('SECRET_KEY not set! Using insecure default. Set SECRET_KEY in .env')
        SECRET_KEY = 'CHANGE_ME_BEFORE_PRODUCTION'
    PORT = int(os.environ.get('PORT', 5001))
    DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

    # -- Database --
    # Dockd's operational tables (ship_history, ship_attempts, override_log)
    # live in Postgres. Set DATABASE_URL to a libpq DSN, e.g.
    # postgresql://dockd_app:...@host:5432/dockd. Required at runtime; the
    # connection pool raises on first use when it is unset.
    DATABASE_URL = os.environ.get('DATABASE_URL', '')
    # Per-container pool ceiling. Single register / single replica, so a
    # small pool is plenty; sized against Postgres max_connections shared
    # with the Sentry stack.
    DB_POOL_MAX = int(os.environ.get('DOCKD_DB_POOL_MAX', 5))

    # -- Printers --
    ZEBRA_PRINTER = os.environ.get('ZEBRA_PRINTER', '')

    # -- USB Scale Hardware IDs --
    SCALE_VENDOR_ID = int(os.environ.get('SCALE_VENDOR_ID', '0x0b67'), 16)
    SCALE_PRODUCT_ID = int(os.environ.get('SCALE_PRODUCT_ID', '0x555e'), 16)

    # -- Label Cache --
    LABEL_MAX_AGE_HOURS = int(os.environ.get('LABEL_MAX_AGE_HOURS', 8))

    # -- Agent Relay --
    AGENT_API_KEY = os.environ.get('AGENT_API_KEY', '')

    # -- ShipRush --
    SHIPRUSH_TOKEN = os.environ.get('SHIPRUSH_TOKEN', '')
    SHIPRUSH_ENDPOINT = os.environ.get(
        'SHIPRUSH_ENDPOINT',
        'https://api.my.shiprush.com/shipmentservice.svc/shipment/ship',
    )

    # -- Logging --
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_DIR = os.environ.get('LOG_DIR', 'logs')
