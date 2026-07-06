"""Postgres-backed operational settings store.

Settings live in the `dockd_settings` table (see alembic 0003): one row per
top-level key, with a JSONB value so nested sections (boxes, stations,
carrier_rules, international, ...) and scalars alike round-trip as native
Python objects.

The store keeps the same interface the JSON version exposed -- `all`,
`get`, `public_subset`, `is_country_banned`, `replace`, `patch` -- so the
admin routes and every service that reads settings through `.get()`
(ShipRush, CarrierEngine, PrinterService, ShippingService) are unchanged.

Reads hit the database fresh each call (no mtime cache), so an admin PUT is
visible immediately to live service code. Values merge OVER
`DEFAULT_SETTINGS`, so a key added to the defaults later is served from the
default until an admin writes it -- the forward-compat property the JSON
`replace()` used to provide.

Secrets (shiprush account GUIDs, tax IDs, station IPs) live inline in this
table rather than a separate encrypted store; they are kept out of
non-admin responses by `public_subset()` and guarded at rest by database
access control.
"""

import logging
from typing import Any, Dict, Optional

from psycopg2.extras import Json

from app.models.database import get_db
from app.services.default_settings import DEFAULT_SETTINGS, SETTINGS_SCHEMA_VERSION

logger = logging.getLogger('dockd.settings')


class SettingsStore:
    """Stateless: every method opens a pooled connection for its work."""

    def __init__(self):
        self.ensure_seeded()

    def ensure_seeded(self) -> None:
        """Seed any missing top-level key from DEFAULT_SETTINGS.

        Idempotent via ON CONFLICT DO NOTHING, so operator-modified values
        are never clobbered and new default keys are backfilled on boot.
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                for key, value in DEFAULT_SETTINGS.items():
                    cur.execute(
                        """INSERT INTO dockd_settings (key, value)
                           VALUES (%s, %s)
                           ON CONFLICT (key) DO NOTHING""",
                        (key, Json(value)),
                    )
            conn.commit()

    # ---- reads ---------------------------------------------------------

    def all(self) -> Dict[str, Any]:
        """Full settings dict: stored rows merged over the defaults."""
        merged = dict(DEFAULT_SETTINGS)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key, value FROM dockd_settings")
                for row in cur.fetchall():
                    merged[row['key']] = row['value']
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM dockd_settings WHERE key = %s", (key,))
                row = cur.fetchone()
        if row is not None:
            return row['value']
        return DEFAULT_SETTINGS.get(key, default)

    def public_subset(self) -> Dict[str, Any]:
        """Return the subset safe to expose to non-admin (logged-in) UI.

        Excludes anything the operator UI doesn't need: account GUIDs,
        carrier-method IDs, override SKUs (those load via a separate
        guarded path), station IPs, and the international tax-ID block
        (EIN / EORI / IOSS / UK VAT are admin-only).
        """
        data = self.all()
        intl = data.get("international") or {}
        return {
            "high_value_threshold": data.get("high_value_threshold"),
            "amazon_methods": data.get("amazon_methods", []),
            "boxes": [
                {"id": b["id"], "label": b.get("label", b["id"])}
                for b in data.get("boxes", [])
            ],
            # Only the booleans the operator UI needs to render an intl
            # pill or block a scan are exposed; tax IDs and the
            # banned-country list are admin-only.
            "international_enabled": bool(intl.get("enabled", False)),
        }

    def is_country_banned(self, country: Optional[str]) -> bool:
        """Return True if `country` is on the banned-destination list.

        Used by ShippingService as a hard pre-label-call gate. The list is
        seeded with OFAC comprehensive-sanctions defaults (CU/IR/KP/SY) and
        tunable by admins. Country is normalized to uppercase ISO 3166
        alpha-2; empty / None always returns False.
        """
        if not country:
            return False
        normalized = str(country).strip().upper()[:2]
        if not normalized:
            return False
        intl = self.get("international") or {}
        banned = intl.get("banned_countries") or []
        return normalized in {str(c).strip().upper()[:2] for c in banned if c}

    # ---- writes --------------------------------------------------------

    def replace(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Overwrite settings wholesale. Merges with defaults first so any
        key the UI didn't send is preserved (a UI bug cannot blow away the
        box library)."""
        merged = dict(DEFAULT_SETTINGS)
        merged.update(new_settings)
        merged["schema_version"] = SETTINGS_SCHEMA_VERSION
        self._upsert_many(merged)
        return self.all()

    def patch(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        """Update the given top-level keys without touching the others."""
        self._upsert_many(partial)
        return self.all()

    def _upsert_many(self, items: Dict[str, Any]) -> None:
        with get_db() as conn:
            with conn.cursor() as cur:
                for key, value in items.items():
                    cur.execute(
                        """INSERT INTO dockd_settings (key, value, updated_at)
                           VALUES (%s, %s, NOW())
                           ON CONFLICT (key) DO UPDATE
                             SET value = EXCLUDED.value, updated_at = NOW()""",
                        (key, Json(value)),
                    )
            conn.commit()


__all__ = ['SettingsStore']
