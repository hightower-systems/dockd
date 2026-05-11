"""JSON-backed operational settings store.

`settings.json` lives at the project root (override via SETTINGS_PATH).
On first access the file is created from `DEFAULT_SETTINGS`. Reads are
re-issued each call (mtime-cached) so a settings PUT from the admin UI
is visible immediately to live service code without restarting Flask.

Writes are atomic: write to a tempfile in the same directory, fsync,
rename over the destination. chmod 600 enforced after every write so
secrets surfaced through the settings UI (if any leak into the JSON)
are not world-readable.
"""

import json
import os
import logging
import threading
from typing import Any, Dict

from app.services.default_settings import DEFAULT_SETTINGS, SETTINGS_SCHEMA_VERSION

logger = logging.getLogger('dockd.settings')

_FILE_MODE = 0o600


class SettingsStore:

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] | None = None
        self._cache_mtime: float | None = None
        self._ensure_initialized()

    @property
    def path(self) -> str:
        return self._path

    def _ensure_initialized(self) -> None:
        if os.path.exists(self._path):
            return
        logger.info("settings.json not found at %s; bootstrapping from defaults", self._path)
        self._atomic_write(DEFAULT_SETTINGS)

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self._path)) or '.'
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self._path}.tmp.{os.getpid()}"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)
        try:
            os.chmod(self._path, _FILE_MODE)
        except OSError as exc:
            logger.warning("could not chmod %s: %s", self._path, exc)

    def _load(self) -> Dict[str, Any]:
        try:
            mtime = os.path.getmtime(self._path)
        except FileNotFoundError:
            self._ensure_initialized()
            mtime = os.path.getmtime(self._path)
        if self._cache is not None and self._cache_mtime == mtime:
            return self._cache
        with open(self._path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        with self._lock:
            self._cache = data
            self._cache_mtime = mtime
        return data

    def all(self) -> Dict[str, Any]:
        """Return the full settings dict (a fresh shallow copy)."""
        return dict(self._load())

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def public_subset(self) -> Dict[str, Any]:
        """Return the subset safe to expose to non-admin (logged-in) UI.

        Excludes anything the operator UI doesn't need: account GUIDs,
        carrier-method IDs, override SKUs (those load via a separate
        guarded path), and station IPs.
        """
        data = self._load()
        return {
            "high_value_threshold": data.get("high_value_threshold"),
            "amazon_methods": data.get("amazon_methods", []),
            "boxes": [
                {"id": b["id"], "label": b.get("label", b["id"])}
                for b in data.get("boxes", [])
            ],
        }

    def replace(self, new_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Overwrite settings wholesale. Merges with defaults to preserve
        any keys the UI didn't send (forward compat for new fields)."""
        merged = dict(DEFAULT_SETTINGS)
        merged.update(new_settings)
        merged["schema_version"] = SETTINGS_SCHEMA_VERSION
        with self._lock:
            self._atomic_write(merged)
            self._cache = None
            self._cache_mtime = None
        return self._load()

    def patch(self, partial: Dict[str, Any]) -> Dict[str, Any]:
        """Update top-level keys without touching others."""
        current = dict(self._load())
        for key, value in partial.items():
            current[key] = value
        current["schema_version"] = SETTINGS_SCHEMA_VERSION
        with self._lock:
            self._atomic_write(current)
            self._cache = None
            self._cache_mtime = None
        return self._load()
