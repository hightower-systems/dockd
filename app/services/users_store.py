"""JSON-backed user credentials store.

`users.json` lives at the project root (override via USERS_PATH). On
first access the file is created from `DEFAULT_USERS_BOOTSTRAP`; the
single bootstrap user is admin/admin with `must_change_password=true`
so a fresh install is forced to set a real password before any
endpoint will respond.

Atomic-write + chmod 600 pattern. Users live in a separate file so the
highest-sensitivity data has the narrowest set of endpoints that
touch it.
"""

import json
import os
import logging
import threading
from typing import List, Optional, Dict, Any

from werkzeug.security import check_password_hash, generate_password_hash

from app.services.default_settings import (
    DEFAULT_USERS_BOOTSTRAP,
    SETTINGS_SCHEMA_VERSION,
    VALID_ROLES,
)

logger = logging.getLogger('dockd.users')

_FILE_MODE = 0o600


class UsersStoreError(Exception):
    pass


def _hash_bootstrap(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Convert the plain-password bootstrap spec into the on-disk shape."""
    out = {"schema_version": spec.get("schema_version", SETTINGS_SCHEMA_VERSION), "users": []}
    for u in spec.get("users", []):
        record = {
            "username": u["username"],
            "role": u.get("role", "user"),
            "password_hash": generate_password_hash(u["password"]),
            "must_change_password": bool(u.get("must_change_password", False)),
        }
        out["users"].append(record)
    return out


class UsersStore:

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._ensure_initialized()

    @property
    def path(self) -> str:
        return self._path

    def _ensure_initialized(self) -> None:
        if os.path.exists(self._path):
            return
        logger.info(
            "users.json not found at %s; bootstrapping with admin/admin "
            "(must_change_password=true)",
            self._path,
        )
        self._atomic_write(_hash_bootstrap(DEFAULT_USERS_BOOTSTRAP))

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
        with open(self._path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _save(self, data: Dict[str, Any]) -> None:
        data["schema_version"] = SETTINGS_SCHEMA_VERSION
        with self._lock:
            self._atomic_write(data)

    def list_users(self) -> List[Dict[str, Any]]:
        """Return usernames + roles + must_change_password, never hashes."""
        data = self._load()
        return [
            {
                "username": u["username"],
                "role": u.get("role", "user"),
                "must_change_password": bool(u.get("must_change_password", False)),
            }
            for u in data.get("users", [])
        ]

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Internal lookup; returns the full record including hash."""
        data = self._load()
        for u in data.get("users", []):
            if u["username"] == username:
                return u
        return None

    def verify(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Check credentials. On success return
        {username, role, must_change_password}."""
        user = self.get_user(username)
        if not user:
            return None
        if not check_password_hash(user.get("password_hash", ""), password):
            return None
        return {
            "username": user["username"],
            "role": user.get("role", "user"),
            "must_change_password": bool(user.get("must_change_password", False)),
        }

    def add_user(
        self,
        username: str,
        password: str,
        role: str,
        must_change_password: bool = True,
    ) -> Dict[str, Any]:
        if role not in VALID_ROLES:
            raise UsersStoreError(f"role must be one of {VALID_ROLES}")
        username = username.strip()
        if not username:
            raise UsersStoreError("username is required")
        if not password or len(password) < 4:
            raise UsersStoreError("password must be at least 4 characters")
        data = self._load()
        if any(u["username"].lower() == username.lower() for u in data.get("users", [])):
            raise UsersStoreError("username already exists")
        data.setdefault("users", []).append({
            "username": username,
            "role": role,
            "password_hash": generate_password_hash(password),
            "must_change_password": bool(must_change_password),
        })
        self._save(data)
        return {
            "username": username,
            "role": role,
            "must_change_password": bool(must_change_password),
        }

    def remove_user(self, username: str) -> None:
        data = self._load()
        users = data.get("users", [])
        admins = [u for u in users if u.get("role") == "admin"]
        target = next((u for u in users if u["username"] == username), None)
        if not target:
            raise UsersStoreError("user not found")
        if target.get("role") == "admin" and len(admins) == 1:
            raise UsersStoreError("cannot remove the only admin")
        data["users"] = [u for u in users if u["username"] != username]
        self._save(data)

    def set_password(
        self,
        username: str,
        password: str,
        must_change_password: Optional[bool] = None,
    ) -> None:
        """Set a user's password.

        When `must_change_password` is None the existing flag is left
        as-is. Admin-initiated resets should pass `True` so the user
        is forced to rotate; self-service changes pass `False`.
        """
        if not password or len(password) < 4:
            raise UsersStoreError("password must be at least 4 characters")
        data = self._load()
        for u in data.get("users", []):
            if u["username"] == username:
                u["password_hash"] = generate_password_hash(password)
                if must_change_password is not None:
                    u["must_change_password"] = bool(must_change_password)
                self._save(data)
                return
        raise UsersStoreError("user not found")

    def change_own_password(
        self,
        username: str,
        current_password: str,
        new_password: str,
    ) -> None:
        """Verify current password, then set the new one and clear the
        must_change_password flag."""
        if not self.verify(username, current_password):
            raise UsersStoreError("current password is incorrect")
        if current_password == new_password:
            raise UsersStoreError("new password must differ from the current one")
        self.set_password(username, new_password, must_change_password=False)

    def set_role(self, username: str, role: str) -> None:
        if role not in VALID_ROLES:
            raise UsersStoreError(f"role must be one of {VALID_ROLES}")
        data = self._load()
        users = data.get("users", [])
        admins = [u for u in users if u.get("role") == "admin"]
        target = next((u for u in users if u["username"] == username), None)
        if not target:
            raise UsersStoreError("user not found")
        if target.get("role") == "admin" and role != "admin" and len(admins) == 1:
            raise UsersStoreError("cannot demote the only admin")
        target["role"] = role
        self._save(data)
