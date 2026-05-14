"""Order-backend interface.

`OrderBackend` is the abstraction `ShippingService` uses to fetch
order details and write ship / void-ship events. The interface mirrors
the contract Sentry-WMS exposes at `/api/v1/dockd/orders/<so_number>`
(load-on-scan, idempotent ship with UUID4 keys, void-ship). A future
NetSuite or other-ERP implementation conforms to the same shape; see
`dockd-plans/sentry-dockd-integration-dockd-side.md` for the design
rationale.

Backend interactions:

    backend.health()            -- liveness check
    backend.get_order(so)       -> OrderData
    backend.confirm_shipped(...)-> ShipResult
    backend.void_ship(...)      -> dict

Errors raise typed exceptions (`NotFoundError`, `AlreadyShippedError`,
etc.) keyed on the `error_kind` Sentry returns. `ShippingService`
catches by type, never parses an error body.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShippingAddress:
    """Structured shipping address as Sentry returns it."""
    name: Optional[str] = None
    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ShippingAddress":
        data = data or {}
        return cls(
            name=data.get("name"),
            line1=data.get("line1"),
            line2=data.get("line2"),
            city=data.get("city"),
            state=data.get("state"),
            postal_code=data.get("postal_code"),
            country=data.get("country"),
            phone=data.get("phone"),
        )


@dataclass(frozen=True)
class CustomsData:
    """Per-item customs declaration data for international shipments.

    Sentry populates this on `OrderItem` only when the destination
    is non-US; domestic orders omit it entirely so payload size stays
    small for the 95% case. Every field is optional at the dataclass
    level so a partially-populated item from Sentry round-trips
    cleanly; ShipRush will reject the label if a required field is
    missing at submit time, which is the intended fail-loud behavior.
    """
    description: Optional[str] = None
    hs_code: Optional[str] = None
    country_of_origin: Optional[str] = None
    unit_weight_oz: Optional[float] = None
    unit_value: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["CustomsData"]:
        if not data:
            return None
        country = data.get("country_of_origin")
        # Normalize country to ISO 3166 alpha-2 uppercase; accept the
        # common 3-letter and lowercase shapes Sentry might emit.
        if country is not None:
            country = str(country).strip().upper()[:2] or None

        def _pos_float(key: str) -> Optional[float]:
            raw = data.get(key)
            if raw is None or raw == "":
                return None
            try:
                v = float(raw)
            except (TypeError, ValueError):
                return None
            return v if v >= 0 else None

        return cls(
            description=(str(data["description"]) if data.get("description") else None),
            hs_code=(str(data["hs_code"]).strip() if data.get("hs_code") else None),
            country_of_origin=country,
            unit_weight_oz=_pos_float("unit_weight_oz"),
            unit_value=_pos_float("unit_value"),
        )


@dataclass(frozen=True)
class OrderItem:
    """One line on an order, as seen by the pack station."""
    external_id: str
    sku: str
    display_name: str
    upc: Optional[str]
    qty: int
    customs: Optional[CustomsData] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderItem":
        return cls(
            external_id=str(data.get("external_id", "")),
            sku=str(data.get("sku", "")),
            display_name=str(data.get("display_name", "") or ""),
            upc=data.get("upc"),
            qty=int(data.get("qty", 0)),
            customs=CustomsData.from_dict(data.get("customs")),
        )


@dataclass(frozen=True)
class OrderData:
    """Canonical order shape consumed by ShippingService.

    Mirrors the dockd-side GET response from Sentry. Frozen because
    callers should not mutate; create a new OrderData for any change.
    """
    so_number: str
    external_id: str
    status: str
    warehouse_id: int
    shippable: bool
    shippable_from_statuses: List[str]
    items: List[OrderItem]
    shipping_address: ShippingAddress
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    ship_method: Optional[str] = None
    memo: Optional[str] = None
    order_total: Optional[float] = None
    customer_shipping_paid: Optional[float] = None
    marketplace: Optional[str] = None
    order_date: Optional[str] = None
    ff_created_at: Optional[str] = None
    # International shipping (Sentry sends these on non-US orders).
    # `currency` is the ISO 4217 code for monetary fields on this
    # order; `duties_paid_by` is 'sender' (DDP) or 'recipient' (DDU)
    # and drives the ShipRush <IncotermsCode> tag.
    currency: str = "USD"
    duties_paid_by: Optional[str] = None
    # Populated only when status == SHIPPED:
    shipped_by: Optional[str] = None
    tracking_number: Optional[str] = None
    carrier: Optional[str] = None
    shipped_at: Optional[str] = None
    station_label: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderData":
        return cls(
            so_number=str(data["so_number"]),
            external_id=str(data.get("external_id", "")),
            status=str(data.get("status", "")),
            warehouse_id=int(data.get("warehouse_id", 0)),
            shippable=bool(data.get("shippable", False)),
            shippable_from_statuses=list(data.get("shippable_from_statuses") or []),
            items=[OrderItem.from_dict(x) for x in (data.get("items") or [])],
            shipping_address=ShippingAddress.from_dict(data.get("shipping_address")),
            customer_name=data.get("customer_name"),
            customer_phone=data.get("customer_phone"),
            ship_method=data.get("ship_method"),
            memo=data.get("memo"),
            order_total=(
                float(data["order_total"])
                if data.get("order_total") is not None
                else None
            ),
            customer_shipping_paid=(
                float(data["customer_shipping_paid"])
                if data.get("customer_shipping_paid") is not None
                else None
            ),
            marketplace=data.get("marketplace"),
            order_date=data.get("order_date"),
            ff_created_at=data.get("ff_created_at"),
            currency=(str(data.get("currency") or "USD").strip().upper()[:3] or "USD"),
            duties_paid_by=(
                str(data["duties_paid_by"]).strip().lower()
                if data.get("duties_paid_by") else None
            ),
            shipped_by=data.get("shipped_by"),
            tracking_number=data.get("tracking_number"),
            carrier=data.get("carrier"),
            shipped_at=data.get("shipped_at"),
            station_label=data.get("station_label"),
        )


@dataclass(frozen=True)
class ShipResult:
    """Sentry POST /ship response shape.

    Note: the dockd plan doc referenced `ship_event_id`; that field
    does not exist in Sentry v1.9. The real response is
    `{status, tracking, shipped_at, fulfillment_id, audit_log_id}`.
    """
    status: str
    tracking: str
    shipped_at: str
    fulfillment_id: int
    audit_log_id: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ShipResult":
        return cls(
            status=str(data.get("status", "")),
            tracking=str(data.get("tracking", "")),
            shipped_at=str(data.get("shipped_at", "")),
            fulfillment_id=int(data.get("fulfillment_id", 0)),
            audit_log_id=int(data.get("audit_log_id", 0)),
        )


@dataclass(frozen=True)
class VoidResult:
    """Sentry POST /void-ship response shape."""
    status: str
    voided_at: str
    audit_log_id: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VoidResult":
        return cls(
            status=str(data.get("status", "")),
            voided_at=str(data.get("voided_at", "")),
            audit_log_id=int(data.get("audit_log_id", 0)),
        )


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """Base for every backend-call failure.

    Carries `error_kind` (string from the backend response when
    available), `details` (the `details` dict from the body, may be
    empty), and `status_code` (HTTP status from the backend response).
    """

    def __init__(
        self,
        error_kind: str = "unknown",
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
        status_code: Optional[int] = None,
    ):
        super().__init__(message or error_kind)
        self.error_kind = error_kind
        self.message = message or error_kind
        self.details = details or {}
        self.status_code = status_code


class NetworkError(BackendError):
    """Connection / timeout failures. Retryable in principle."""


class NotFoundError(BackendError):
    """Sentry 404 -- unknown SO, or out-of-scope warehouse (conflated)."""


class AlreadyShippedError(BackendError):
    """Sentry 409 + error_kind='already_shipped'. `details` carries
    existing_tracking / carrier / shipped_at / shipped_by."""


class NotInShippableStatusError(BackendError):
    """Sentry 410 + error_kind='not_in_shippable_status'. `details`
    carries current_status + allowed_statuses."""


class IdempotencyMismatchError(BackendError):
    """Sentry 409 + error_kind='idempotency_key_reused_with_different_body'."""


class IdempotencyLockTimeoutError(BackendError):
    """Sentry 503 + error_kind='idempotency_lock_timeout'. Retryable."""


class UnknownOperatorError(BackendError):
    """Sentry 422 + error_kind='unknown_operator'. The username does
    not resolve to a backend user record."""


class NotShippedError(BackendError):
    """Sentry 409 + error_kind='not_shipped' on the void route. The
    SO is not currently in SHIPPED status."""


class InvalidBodyError(BackendError):
    """Sentry 422 + error_kind='invalid_body'. Programmer error; the
    body shape doesn't match the backend's Pydantic model."""


class RateLimitedError(BackendError):
    """Sentry 429 (limiter hit). Retryable after backoff."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class OrderBackend(Protocol):
    """Backend interface for dockd's order surface.

    Implementations:
    - `SentryBackend` (app/services/backend/sentry.py) -- v1 default
    - A future `NetSuiteBackend` would adapt the preserved legacy
      code in `dockd-plans/netsuite-legacy/` to this Protocol.

    `Protocol` (PEP 544) is used instead of ABC so mocks need only
    duck-typing; tests provide a `MagicMock` directly with no
    inheritance.
    """

    def get_order(self, so_number: str) -> OrderData:
        ...

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
        ...

    def void_ship(
        self,
        so_number: str,
        *,
        reason: str,
        operator_username: str,
        idempotency_key: str,
    ) -> VoidResult:
        ...

    def health(self) -> bool:
        ...
