"""baseline: ship_history, ship_attempts, override_log

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-06

The initial Postgres schema for Dockd's operational tables, translated from
the SQLite DDL that lived in app/models/database.py (init_ship_db /
init_ship_attempts_db / init_override_db). Authored as raw DDL because Dockd
has no SQLAlchemy models.

Translation notes:
- INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL PRIMARY KEY
- TEXT DEFAULT (datetime('now','localtime')) -> TIMESTAMPTZ DEFAULT NOW()
- REAL -> DOUBLE PRECISION
- carrier_switched / manual_link stay SMALLINT (code writes 1/0; insert-only
  metrics, so a boolean cast would be churn for no gain)
- CHECK constraints on ship_attempts.operation / .status carried over verbatim
- override_log."user" is quoted -- user is a reserved word in Postgres
"""
from alembic import op

revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ship_history (
            id                      BIGSERIAL PRIMARY KEY,
            order_number            TEXT NOT NULL,
            fulfillment_id          TEXT,
            items_skus              TEXT,
            box_id                  TEXT,
            dims                    TEXT,
            weight                  DOUBLE PRECISION,
            shipping_cost           DOUBLE PRECISION,
            tracking                TEXT,
            carrier                 TEXT,
            ship_method             TEXT,
            shipped_by              TEXT,
            shipped_at              TIMESTAMPTZ DEFAULT NOW(),
            ff_created_at           TIMESTAMPTZ,
            order_loaded_at         TIMESTAMPTZ,
            fulfillment_age_minutes DOUBLE PRECISION,
            ship_speed_seconds      DOUBLE PRECISION,
            carrier_switched        SMALLINT DEFAULT 0,
            station_id              TEXT,
            station_label           TEXT,
            external_id             TEXT,
            customer_shipping_paid  DOUBLE PRECISION,
            order_total             DOUBLE PRECISION,
            sentry_audit_log_id     BIGINT,
            sentry_fulfillment_id   BIGINT,
            manual_link             SMALLINT DEFAULT 0,
            idempotency_key         TEXT,
            voided_at               TIMESTAMPTZ,
            void_reason             TEXT,
            destination_country     TEXT,
            customs_value           DOUBLE PRECISION,
            customs_currency        TEXT,
            hs_codes                TEXT
        );
        CREATE INDEX idx_ship_history_order ON ship_history (order_number);
        CREATE INDEX idx_ship_history_date  ON ship_history (shipped_at);
        CREATE INDEX idx_ship_history_idem  ON ship_history (idempotency_key);
        """
    )

    op.execute(
        """
        CREATE TABLE ship_attempts (
            id                  BIGSERIAL PRIMARY KEY,
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
            last_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX idx_ship_attempts_status ON ship_attempts (status);
        CREATE INDEX idx_ship_attempts_recoverable ON ship_attempts (status, last_attempt_at);
        CREATE INDEX idx_ship_attempts_so ON ship_attempts (so_number);
        """
    )

    op.execute(
        """
        CREATE TABLE override_log (
            id            BIGSERIAL PRIMARY KEY,
            order_number  TEXT,
            "user"        TEXT,
            item_name     TEXT,
            sku           TEXT,
            station       TEXT,
            override_type TEXT,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS override_log;")
    op.execute("DROP TABLE IF EXISTS ship_attempts;")
    op.execute("DROP TABLE IF EXISTS ship_history;")
