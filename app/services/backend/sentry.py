"""SentryBackend -- HTTP client for the Sentry-WMS dockd surface.

Wire shape (Sentry side, frozen v1.9):

    GET  /api/v1/dockd/orders/<so_number>
    POST /api/v1/dockd/orders/<so_number>/ship
    POST /api/v1/dockd/orders/<so_number>/void-ship
    GET  /api/health

Auth: `X-WMS-Token: <per-station bearer token>` on every dockd-scope
request. The token is supplied per-call via a `get_token` callable so
the backend can be shared across stations (each station's browser
forwards its own token through the Flask layer; v0.2.0 reads a single
token from `DOCKD_SENTRY_TOKEN` env as an interim source until the
scale-agent v2 + browser /whoami flow lands in v0.4.0).

Errors: every Sentry 4xx / 5xx with an `error_kind` body maps to a
typed exception from `app.services.backend`. Network/timeout errors
raise `NetworkError`. Unmapped `error_kind` values raise the generic
`BackendError` with the raw kind preserved.
"""

import logging
from typing import Any, Callable, Dict, Optional

import httpx

from app.services.backend import (
    AlreadyShippedError,
    BackendError,
    IdempotencyLockTimeoutError,
    IdempotencyMismatchError,
    InvalidBodyError,
    NetworkError,
    NotFoundError,
    NotInShippableStatusError,
    NotShippedError,
    OrderData,
    RateLimitedError,
    ShipResult,
    UnknownOperatorError,
    VoidResult,
)

logger = logging.getLogger('dockd.backend.sentry')


class SentryBackend:
    """Sentry-WMS implementation of `OrderBackend`.

    Construction:

        SentryBackend(
            base_url="https://sentry.example.com",
            get_token=lambda: os.environ["DOCKD_SENTRY_TOKEN"],
        )

    `get_token` is invoked per-request so token rotation does not
    require rebuilding the client.
    """

    def __init__(
        self,
        base_url: str,
        get_token: Callable[[], str],
        timeout: float = 10.0,
        client: Optional[httpx.Client] = None,
    ):
        if not base_url:
            raise ValueError("SentryBackend requires a non-empty base_url")
        self._base_url = base_url.rstrip('/')
        self._get_token = get_token
        # Test fixtures inject a pre-configured httpx.Client; production
        # path builds one here. `verify=True` is hardcoded; no env knob
        # disables TLS validation by design.
        self._client = client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            verify=True,
        )

    # ---- public surface -------------------------------------------------

    def get_order(self, so_number: str) -> OrderData:
        resp = self._request(
            "GET",
            f"/api/v1/dockd/orders/{so_number}",
        )
        return OrderData.from_dict(resp.json())

    def confirm_shipped(
        self,
        so_number: str,
        *,
        tracking: str,
        carrier: str,
        ship_method: Optional[str],
        operator_username: str,
        shipping_cost: Optional[float],
        weight: Optional[float],
        dims: Optional[Dict[str, float]],
        manual_link: bool,
        idempotency_key: str,
    ) -> ShipResult:
        body: Dict[str, Any] = {
            "tracking": tracking,
            "carrier": carrier,
            "operator_username": operator_username,
            "manual_link": bool(manual_link),
            "idempotency_key": idempotency_key,
        }
        # Optional fields: Sentry's Pydantic model is `extra='forbid'`,
        # so we only send keys that have values.
        if ship_method:
            body["ship_method"] = ship_method
        if shipping_cost is not None:
            # Sentry expects a Decimal-shaped value (max_digits=12,
            # decimal_places=2). Send as a JSON number; the Pydantic
            # Decimal field accepts it.
            body["shipping_cost"] = round(float(shipping_cost), 2)
        if weight is not None and weight > 0:
            body["weight"] = float(weight)
        if dims:
            body["dims"] = {
                "l": float(dims["l"]),
                "w": float(dims["w"]),
                "h": float(dims["h"]),
            }

        resp = self._request(
            "POST",
            f"/api/v1/dockd/orders/{so_number}/ship",
            json=body,
        )
        return ShipResult.from_dict(resp.json())

    def void_ship(
        self,
        so_number: str,
        *,
        reason: str,
        operator_username: str,
        idempotency_key: str,
    ) -> VoidResult:
        body = {
            "reason": reason,
            "operator_username": operator_username,
            "idempotency_key": idempotency_key,
        }
        resp = self._request(
            "POST",
            f"/api/v1/dockd/orders/{so_number}/void-ship",
            json=body,
        )
        return VoidResult.from_dict(resp.json())

    def health(self) -> bool:
        try:
            resp = self._client.get("/api/health", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError as exc:
            logger.warning("Sentry health check failed: %s", exc)
            return False

    def close(self) -> None:
        """Release the underlying connection pool. Optional; the Flask
        app keeps a single client alive for the process lifetime."""
        try:
            self._client.close()
        except Exception:
            pass

    # ---- internals -----------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "X-WMS-Token": self._get_token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        try:
            resp = self._client.request(
                method,
                path,
                json=json,
                headers=self._headers(),
            )
        except httpx.TimeoutException as exc:
            raise NetworkError(
                error_kind="timeout",
                message=f"{method} {path}: timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(
                error_kind="network_error",
                message=f"{method} {path}: {exc}",
            ) from exc

        if 200 <= resp.status_code < 300:
            return resp

        self._raise_for_status(resp, method, path)
        # _raise_for_status always raises; this is unreachable but
        # satisfies the type checker.
        raise BackendError(status_code=resp.status_code)

    def _raise_for_status(self, resp: httpx.Response, method: str, path: str) -> None:
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        kind = str(body.get("error_kind") or "unknown")
        message = str(body.get("message") or "")
        details = body.get("details") or {}
        if not isinstance(details, dict):
            details = {}

        status = resp.status_code
        common_kwargs = dict(
            error_kind=kind,
            message=message,
            details=details,
            status_code=status,
        )

        # Map (status, error_kind) -> typed exception. Order matters
        # for the 409 cluster.
        if status == 404:
            raise NotFoundError(**common_kwargs)
        if status == 409 and kind == "already_shipped":
            raise AlreadyShippedError(**common_kwargs)
        if status == 409 and kind == "idempotency_key_reused_with_different_body":
            raise IdempotencyMismatchError(**common_kwargs)
        if status == 409 and kind == "not_shipped":
            raise NotShippedError(**common_kwargs)
        if status == 410:
            raise NotInShippableStatusError(**common_kwargs)
        if status == 422 and kind == "unknown_operator":
            raise UnknownOperatorError(**common_kwargs)
        if status == 422:
            raise InvalidBodyError(**common_kwargs)
        if status == 429:
            raise RateLimitedError(**common_kwargs)
        if status == 503 and kind == "idempotency_lock_timeout":
            raise IdempotencyLockTimeoutError(**common_kwargs)
        if status >= 500:
            raise NetworkError(**common_kwargs)

        # Unknown error_kind / unmapped status; surface as the generic
        # BackendError with the raw kind preserved.
        logger.warning(
            "Sentry returned unmapped error: %s %s %d kind=%s",
            method, path, status, kind,
        )
        raise BackendError(**common_kwargs)
