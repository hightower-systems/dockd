"""dockd_settings

Revision ID: 0003_dockd_settings
Revises: 0001_baseline
Create Date: 2026-07-06

Moves Dockd's operational settings off the JSON file
(app/services/settings.py) into Postgres. One row per top-level settings
key, with a JSONB value so nested/typed sections (boxes, stations,
carrier_rules, international, ...) and plain scalars alike round-trip
without stringifying.

Mirrors Sentry-WMS's app_settings key/value mechanism, but with a JSONB
value (Sentry's own user_dashboard_preferences.chart_order JSONB is the
precedent) because Dockd's config is structured rather than flat strings.

Secrets (shiprush account GUIDs, tax IDs, station IPs) stay inline here
rather than in a separate encrypted store -- guarded by DB access and the
SettingsStore.public_subset() redaction that already hides them from
non-admin callers.
"""
from alembic import op

revision = '0003_dockd_settings'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dockd_settings (
            key        VARCHAR(100) PRIMARY KEY,
            value      JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dockd_settings;")
