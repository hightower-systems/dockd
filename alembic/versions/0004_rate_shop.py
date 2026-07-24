"""rate_shop

Revision ID: 0004_rate_shop
Revises: 0003_dockd_settings
Create Date: 2026-07-22

Records what rate shopping quoted alongside what the carrier actually
charged.

`shipping_cost` (0001) is the charge parsed out of the LABEL response
(<CarrierRate>), so it has only ever been knowable after the money was
spent. v2 quotes first via /shipment/rateshopping, which makes the pair
comparable for the first time.

The gap is worth storing for a reason beyond curiosity: `rate_shop()` and
`generate_label()` are two separate XML builders aimed at the same
carrier. If they ever disagree about dims, weight, packaging code, or
which shipping account to use, the symptom is a quote that does not match
the charge -- and nothing else in the system would notice. quoted_cost vs
shipping_cost is the tripwire.

`chosen_reason` stores the sentence rate_select.py produced, so a decision
is still explainable months later without re-deriving it from settings
that have since changed.

Columns are nullable with no backfill and no default: rows written before
this migration, and rows written while rate shopping is unavailable, are
legitimately unknown rather than zero. Zero would read as "quoted free".
"""
from alembic import op

revision = '0004_rate_shop'
down_revision = '0003_dockd_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ship_history
            ADD COLUMN quoted_cost     DOUBLE PRECISION,
            ADD COLUMN quoted_service  TEXT,
            ADD COLUMN chosen_reason   TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ship_history
            DROP COLUMN IF EXISTS quoted_cost,
            DROP COLUMN IF EXISTS quoted_service,
            DROP COLUMN IF EXISTS chosen_reason;
        """
    )
