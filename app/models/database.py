"""SQLite database helpers for Dockd.

Two databases:
- shipping_history.db: order tracking, carrier costs, timing metrics
- override.db: audit trail of manual overrides
"""

import os
import sys
import sqlite3
import logging

logger = logging.getLogger('dockd.database')


def resource_path(relative_path):
    """Resolve path for PyInstaller bundles or normal execution."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(base_path, relative_path)


SHIP_DB_PATH = resource_path('shipping_history.db')
OVERRIDE_DB_PATH = resource_path('override.db')


def _get_db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def get_ship_db():
    return _get_db(SHIP_DB_PATH)


def get_override_db():
    return _get_db(OVERRIDE_DB_PATH)


def init_ship_db():
    conn = get_ship_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ship_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL,
            fulfillment_id TEXT,
            items_skus TEXT,
            box_id TEXT,
            dims TEXT,
            weight REAL,
            shipping_cost REAL,
            tracking TEXT,
            carrier TEXT,
            ship_method TEXT,
            shipped_by TEXT,
            shipped_at TEXT DEFAULT (datetime('now','localtime')),
            ff_created_at TEXT,
            order_loaded_at TEXT,
            fulfillment_age_minutes REAL,
            ship_speed_seconds REAL,
            carrier_switched INTEGER DEFAULT 0,
            station_id TEXT,
            station_label TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ship_history_order
            ON ship_history(order_number);
        CREATE INDEX IF NOT EXISTS idx_ship_history_date
            ON ship_history(shipped_at);
    """)
    # Idempotent migration: for any pre-v0.3.0 DB the CREATE IF NOT
    # EXISTS above is a no-op and the new columns are missing. SQLite
    # has no ALTER TABLE ADD COLUMN IF NOT EXISTS, so we discover via
    # PRAGMA + add when absent.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ship_history)").fetchall()}
    for col, ddl in (
        ("station_id", "ALTER TABLE ship_history ADD COLUMN station_id TEXT"),
        ("station_label", "ALTER TABLE ship_history ADD COLUMN station_label TEXT"),
    ):
        if col not in existing:
            conn.execute(ddl)
    conn.commit()
    conn.close()
    logger.info("Shipping history DB initialized")


def init_override_db():
    conn = get_override_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS override_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT,
            user TEXT,
            item_name TEXT,
            sku TEXT,
            station TEXT,
            override_type TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.close()
    logger.info("Override DB initialized")


def init_all_dbs():
    init_ship_db()
    init_override_db()
