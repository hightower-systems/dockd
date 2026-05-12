"""Ship-attempts store: crash-recovery idempotency for backend writes.

`ShipAttemptsStore` is the persistent counterpart to dockd's
per-request UUID4 idempotency keys. Every backend write (ship, void,
manual_link) opens a row in `ship_attempts` BEFORE the network call;
the row is then transitioned to `success`, `unknown`, or `rejected`
based on the outcome.

The point of the table is process-crash recovery. If the operator
clicks Ship and dockd crashes between the ShipRush label landing and
Sentry's confirm response, the operator's tracking number is on a
printed label but Sentry never wrote the ship. The next dockd start
reads any `pending` or `unknown` row and retries with the same
idempotency key; Sentry's own `dockd_idempotency` table either
replays the cached response (the original committed before the
crash) or re-executes the write (the original rolled back).
"""

import hashlib
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from app.models.database import get_ship_db

logger = logging.getLogger('dockd.ship_attempts')


_VALID_OPERATIONS = {'ship', 'void', 'manual_link'}
_VALID_STATUSES = {'pending', 'success', 'unknown', 'rejected'}


def _canonical_body(body: Dict[str, Any]) -> str:
    """Stable JSON encoding for body hashing.

    Keys sorted, separators tight, no whitespace; ensures two runs
    that build the same logical body hash to the same digest.
    """
    return json.dumps(body, sort_keys=True, separators=(',', ':'), default=str)


def _body_sha256(body: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_body(body).encode('utf-8')).hexdigest()


def new_idempotency_key() -> str:
    """Mint a fresh UUID4 string. Centralized so all callers go
    through the same place for testability + grep-ability."""
    return str(uuid.uuid4())


class ShipAttemptsStore:
    """Thin SQLite-backed wrapper around the `ship_attempts` table.

    Single connection per operation; lock guards the rare
    write-after-read pattern (mark_*). The table itself sits in
    `shipping_history.db` alongside `ship_history` so a backup of
    one file captures everything.
    """

    def __init__(self):
        self._lock = threading.Lock()

    # ---- write path ----------------------------------------------------

    def insert_pending(
        self,
        *,
        idempotency_key: str,
        operation: str,
        so_number: str,
        request_body: Dict[str, Any],
    ) -> None:
        """Open a pending row right before the backend call.

        UNIQUE on `idempotency_key` is the safety belt: a programmer
        bug that reuses a key under the same store raises
        sqlite3.IntegrityError, which `mark_*` callers do not catch.
        """
        if operation not in _VALID_OPERATIONS:
            raise ValueError(f"operation must be one of {_VALID_OPERATIONS}")
        body_text = _canonical_body(request_body)
        body_hash = _body_sha256(request_body)
        with self._lock:
            conn = get_ship_db()
            try:
                conn.execute(
                    """INSERT INTO ship_attempts
                       (idempotency_key, operation, so_number,
                        request_body, request_body_sha256, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (idempotency_key, operation, so_number, body_text, body_hash),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_success(
        self,
        idempotency_key: str,
        response_body: Optional[Dict[str, Any]] = None,
        response_status: int = 200,
    ) -> None:
        self._set_status(
            idempotency_key,
            status='success',
            response_body=response_body,
            response_status=response_status,
            error_kind=None,
        )

    def mark_unknown(
        self,
        idempotency_key: str,
        error_kind: str = 'network_error',
        message: str = '',
    ) -> None:
        """Network / 5xx / timeout. Status is unclear; retry on the
        next dockd boot using the same key."""
        body = {'error_kind': error_kind, 'message': message} if message else None
        self._set_status(
            idempotency_key,
            status='unknown',
            response_body=body,
            response_status=None,
            error_kind=error_kind,
        )

    def mark_rejected(
        self,
        idempotency_key: str,
        error_kind: str,
        message: str = '',
        details: Optional[Dict[str, Any]] = None,
        response_status: Optional[int] = None,
    ) -> None:
        """4xx with a typed error_kind. The backend has spoken; do
        not retry."""
        body = {'error_kind': error_kind, 'message': message, 'details': details or {}}
        self._set_status(
            idempotency_key,
            status='rejected',
            response_body=body,
            response_status=response_status,
            error_kind=error_kind,
        )

    def _set_status(
        self,
        idempotency_key: str,
        *,
        status: str,
        response_body: Optional[Dict[str, Any]],
        response_status: Optional[int],
        error_kind: Optional[str],
    ) -> None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"status must be one of {_VALID_STATUSES}")
        body_text = json.dumps(response_body) if response_body is not None else None
        with self._lock:
            conn = get_ship_db()
            try:
                conn.execute(
                    """UPDATE ship_attempts
                          SET status = ?,
                              response_body = ?,
                              response_status = ?,
                              error_kind = ?,
                              attempt_count = attempt_count + 1,
                              last_attempt_at = datetime('now', 'localtime')
                        WHERE idempotency_key = ?""",
                    (status, body_text, response_status, error_kind, idempotency_key),
                )
                conn.commit()
            finally:
                conn.close()

    # ---- read path -----------------------------------------------------

    def get(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        conn = get_ship_db()
        try:
            row = conn.execute(
                "SELECT * FROM ship_attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def find_recoverable(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Rows in `pending` or `unknown` state, oldest first.

        Called at app boot to retry mid-ship attempts that the
        process did not finish before the previous crash. The limit
        caps how many we drain in one boot so a bad batch cannot
        delay startup forever.
        """
        conn = get_ship_db()
        try:
            rows = conn.execute(
                """SELECT * FROM ship_attempts
                    WHERE status IN ('pending', 'unknown')
                    ORDER BY created_at ASC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def list_recent(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Recent attempts for admin observability (newest first)."""
        conn = get_ship_db()
        try:
            rows = conn.execute(
                """SELECT * FROM ship_attempts
                    ORDER BY created_at DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # ---- maintenance ---------------------------------------------------

    def prune_terminal(self, *, older_than_days: int = 14) -> int:
        """Drop `success` and `rejected` rows older than the cutoff.

        Pending and unknown rows are NEVER auto-pruned -- they signal
        a real issue and should be investigated. Returns the number
        of rows deleted.

        Safe to call from a cron / scheduled task; not invoked
        automatically by dockd today.
        """
        cutoff_sql = f"datetime('now', 'localtime', '-{int(older_than_days)} days')"
        with self._lock:
            conn = get_ship_db()
            try:
                cur = conn.execute(
                    f"""DELETE FROM ship_attempts
                         WHERE status IN ('success', 'rejected')
                           AND created_at < {cutoff_sql}"""
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()


__all__ = [
    'ShipAttemptsStore',
    'new_idempotency_key',
    '_body_sha256',
    '_canonical_body',
]
