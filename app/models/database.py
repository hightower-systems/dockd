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
    # CREATE TABLE first; indexes that reference v0.4.0+ columns are
    # created AFTER the migration block below so they line up with the
    # current schema even on older databases.
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
            station_label TEXT,
            external_id TEXT,
            customer_shipping_paid REAL,
            order_total REAL,
            sentry_audit_log_id INTEGER,
            sentry_fulfillment_id INTEGER,
            manual_link INTEGER DEFAULT 0,
            idempotency_key TEXT,
            voided_at TEXT,
            void_reason TEXT,
            destination_country TEXT,
            customs_value REAL,
            customs_currency TEXT,
            hs_codes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ship_history_order
            ON ship_history(order_number);
        CREATE INDEX IF NOT EXISTS idx_ship_history_date
            ON ship_history(shipped_at);
    """)
    # Idempotent migration for older databases. SQLite has no
    # ALTER TABLE ADD COLUMN IF NOT EXISTS, so we discover existing
    # columns via PRAGMA + add when absent.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ship_history)").fetchall()}
    for col, ddl in (
        ("station_id",            "ALTER TABLE ship_history ADD COLUMN station_id TEXT"),
        ("station_label",         "ALTER TABLE ship_history ADD COLUMN station_label TEXT"),
        ("external_id",           "ALTER TABLE ship_history ADD COLUMN external_id TEXT"),
        ("customer_shipping_paid","ALTER TABLE ship_history ADD COLUMN customer_shipping_paid REAL"),
        ("order_total",           "ALTER TABLE ship_history ADD COLUMN order_total REAL"),
        ("sentry_audit_log_id",   "ALTER TABLE ship_history ADD COLUMN sentry_audit_log_id INTEGER"),
        ("sentry_fulfillment_id", "ALTER TABLE ship_history ADD COLUMN sentry_fulfillment_id INTEGER"),
        ("manual_link",           "ALTER TABLE ship_history ADD COLUMN manual_link INTEGER DEFAULT 0"),
        ("idempotency_key",       "ALTER TABLE ship_history ADD COLUMN idempotency_key TEXT"),
        ("voided_at",             "ALTER TABLE ship_history ADD COLUMN voided_at TEXT"),
        ("void_reason",           "ALTER TABLE ship_history ADD COLUMN void_reason TEXT"),
        ("destination_country",   "ALTER TABLE ship_history ADD COLUMN destination_country TEXT"),
        ("customs_value",         "ALTER TABLE ship_history ADD COLUMN customs_value REAL"),
        ("customs_currency",      "ALTER TABLE ship_history ADD COLUMN customs_currency TEXT"),
        ("hs_codes",              "ALTER TABLE ship_history ADD COLUMN hs_codes TEXT"),
    ):
        if col not in existing:
            conn.execute(ddl)
    # Index on idempotency_key created here so older DBs that just
    # had the column added by the ALTER block can still be indexed.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ship_history_idem ON ship_history(idempotency_key)"
    )
    conn.commit()
    conn.close()
    logger.info("Shipping history DB initialized")


def init_ship_attempts_db():
    """Crash-recovery idempotency table.

    Persists `(idempotency_key, request_body)` BEFORE every backend
    write so a process crash mid-ship still leaves the operator with
    a recoverable record. On restart, every row with status in
    ('pending', 'unknown') is retried with the same key; Sentry's
    own dockd_idempotency table replays the cached response if the
    original committed, or re-executes if it didn't.

    Status values:
    - pending  -- row inserted, network call has not yet returned
    - success  -- backend returned 2xx; row is now archival
    - unknown  -- timeout / 5xx / network error; status unclear, retryable
    - rejected -- 4xx with a typed error_kind; not retryable on its own
    """
    conn = get_ship_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ship_attempts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key     TEXT UNIQUE NOT NULL,
            operation           TEXT NOT NULL
                                CHECK (operation IN ('ship', 'void', 'manual_link')),
            so_number           TEXT NOT NULL,
            request_body        TEXT NOT NULL,
            request_body_sha256 TEXT NOT NULL,
            status              TEXT NOT NULL
                                CHECK (status IN ('pending', 'success', 'unknown', 'rejected')),
            response_body       TEXT,
            response_status     INTEGER,
            error_kind          TEXT,
            attempt_count       INTEGER NOT NULL DEFAULT 1,
            last_attempt_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            created_at          TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_ship_attempts_status
            ON ship_attempts(status);
        CREATE INDEX IF NOT EXISTS idx_ship_attempts_recoverable
            ON ship_attempts(status, last_attempt_at);
        CREATE INDEX IF NOT EXISTS idx_ship_attempts_so
            ON ship_attempts(so_number);
    """)
    conn.commit()
    conn.close()
    logger.info("Ship attempts DB initialized")


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
    init_ship_attempts_db()
    init_override_db()
